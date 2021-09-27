# -*- coding: utf-8 -*-
# Copyright (c) 2015, Frappe Technologies and contributors
# License: MIT. See LICENSE

import traceback
import json

from rq.timeouts import JobTimeoutException
import smtplib
import quopri
from email.parser import Parser
from email.policy import SMTPUTF8
from html2text import html2text
from six.moves import html_parser as HTMLParser

import frappe
from frappe import _, safe_encode, task
from frappe.model.document import Document
from frappe.email.queue import get_unsubcribed_url, get_unsubscribe_message
from frappe.email.email_body import add_attachment, get_formatted_html, get_email
from frappe.utils import cint, split_emails, add_days, nowdate, cstr
from frappe.email.doctype.email_account.email_account import EmailAccount


MAX_RETRY_COUNT = 3
class EmailQueue(Document):
	DOCTYPE = 'Email Queue'

	def set_recipients(self, recipients):
		self.set("recipients", [])
		for r in recipients:
			self.append("recipients", {"recipient":r, "status":"Not Sent"})

	def on_trash(self):
		self.prevent_email_queue_delete()

	def prevent_email_queue_delete(self):
		if frappe.session.user != 'Administrator':
			frappe.throw(_('Only Administrator can delete Email Queue'))

	def get_duplicate(self, recipients):
		values = self.as_dict()
		del values['name']
		duplicate = frappe.get_doc(values)
		duplicate.set_recipients(recipients)
		return duplicate

	@classmethod
	def new(cls, doc_data, ignore_permissions=False):
		data = doc_data.copy()
		if not data.get('recipients'):
			return

		recipients = data.pop('recipients')
		doc = frappe.new_doc(cls.DOCTYPE)
		doc.update(data)
		doc.set_recipients(recipients)
		doc.insert(ignore_permissions=ignore_permissions)
		return doc

	@classmethod
	def find(cls, name):
		return frappe.get_doc(cls.DOCTYPE, name)

	@classmethod
	def find_one_by_filters(cls, **kwargs):
		name = frappe.db.get_value(cls.DOCTYPE, kwargs)
		return cls.find(name) if name else None

	def update_db(self, commit=False, **kwargs):
		frappe.db.set_value(self.DOCTYPE, self.name, kwargs)
		if commit:
			frappe.db.commit()

	def update_status(self, status, commit=False, **kwargs):
		self.update_db(status = status, commit = commit, **kwargs)
		if self.communication:
			communication_doc = frappe.get_doc('Communication', self.communication)
			communication_doc.set_delivery_status(commit=commit)

	@property
	def cc(self):
		return (self.show_as_cc and self.show_as_cc.split(",")) or []

	@property
	def to(self):
		return [r.recipient for r in self.recipients if r.recipient not in self.cc]

	@property
	def attachments_list(self):
		return json.loads(self.attachments) if self.attachments else []

	def get_email_account(self):
		if self.email_account:
			return frappe.get_doc('Email Account', self.email_account)

		return EmailAccount.find_outgoing(
			match_by_email = self.sender, match_by_doctype = self.reference_doctype)

	def is_to_be_sent(self):
		return self.status in ['Not Sent','Partially Sent']

	def can_send_now(self):
		hold_queue = (cint(frappe.defaults.get_defaults().get("hold_queue"))==1)
		if frappe.are_emails_muted() or not self.is_to_be_sent() or hold_queue:
			return False

		return True

	def send(self, is_background_task=False):
		""" Send emails to recipients.
		"""
		if not self.can_send_now():
			frappe.db.rollback()
			return

		with SendMailContext(self, is_background_task) as ctx:
			message = None
			for recipient in self.recipients:
				if not recipient.is_mail_to_be_sent():
					continue

				message = ctx.build_message(recipient.recipient)
				if not frappe.flags.in_test:
					ctx.smtp_session.sendmail(from_addr=self.sender, to_addrs=recipient.recipient, msg=message)
				ctx.add_to_sent_list(recipient)

			if frappe.flags.in_test:
				frappe.flags.sent_mail = message
				return

			if ctx.email_account_doc.append_emails_to_sent_folder and ctx.sent_to:
				ctx.email_account_doc.append_email_to_sent_folder(message)


@task(queue = 'short')
def send_mail(email_queue_name, is_background_task=False):
	"""This is equalent to EmqilQueue.send.

	This provides a way to make sending mail as a background job.
	"""
	record = EmailQueue.find(email_queue_name)
	record.send(is_background_task=is_background_task)

class SendMailContext:
	def __init__(self, queue_doc: Document, is_background_task: bool = False):
		self.queue_doc = queue_doc
		self.is_background_task = is_background_task
		self.email_account_doc = queue_doc.get_email_account()
		self.smtp_server = self.email_account_doc.get_smtp_server()
		self.sent_to = [rec.recipient for rec in self.queue_doc.recipients if rec.is_main_sent()]

	def __enter__(self):
		self.queue_doc.update_status(status='Sending', commit=True)
		return self

	def __exit__(self, exc_type, exc_val, exc_tb):
		exceptions = [
			smtplib.SMTPServerDisconnected,
			smtplib.SMTPAuthenticationError,
			smtplib.SMTPRecipientsRefused,
			smtplib.SMTPConnectError,
			smtplib.SMTPHeloError,
			JobTimeoutException
		]

		self.smtp_server.quit()
		self.log_exception(exc_type, exc_val, exc_tb)

		if exc_type in exceptions:
			email_status = (self.sent_to and 'Partially Sent') or 'Not Sent'
			self.queue_doc.update_status(status = email_status, commit = True)
		elif exc_type:
			if self.queue_doc.retry < MAX_RETRY_COUNT:
				update_fields = {'status': 'Not Sent', 'retry': self.queue_doc.retry + 1}
			else:
				update_fields = {'status': (self.sent_to and 'Partially Errored') or 'Error'}
			self.queue_doc.update_status(**update_fields, commit = True)
		else:
			email_status = self.is_mail_sent_to_all() and 'Sent'
			email_status = email_status or (self.sent_to and 'Partially Sent') or 'Not Sent'

			update_fields = {'status': email_status}
			if self.email_account_doc.is_exists_in_db():
				update_fields['email_account'] = self.email_account_doc.name
			else:
				update_fields['email_account'] = None

			self.queue_doc.update_status(**update_fields, commit = True)

	def log_exception(self, exc_type, exc_val, exc_tb):
		if exc_type:
			traceback_string = "".join(traceback.format_tb(exc_tb))
			traceback_string += f"\n Queue Name: {self.queue_doc.name}"

			if self.is_background_task:
				frappe.log_error(title = 'frappe.email.queue.flush', message = traceback_string)
			else:
				frappe.log_error(message = traceback_string)

	@property
	def smtp_session(self):
		if frappe.flags.in_test:
			return
		return self.smtp_server.session

	def add_to_sent_list(self, recipient):
		# Update recipient status
		recipient.update_db(status='Sent', commit=True)
		self.sent_to.append(recipient.recipient)

	def is_mail_sent_to_all(self):
		return sorted(self.sent_to) == sorted([rec.recipient for rec in self.queue_doc.recipients])

	def get_message_object(self, message):
		return Parser(policy=SMTPUTF8).parsestr(message)

	def message_placeholder(self, placeholder_key):
		map = {
			'tracker': '<!--email open check-->',
			'unsubscribe_url': '<!--unsubscribe url-->',
			'cc': '<!--cc message-->',
			'recipient': '<!--recipient-->',
		}
		return map.get(placeholder_key)

	def build_message(self, recipient_email):
		"""Build message specific to the recipient.
		"""
		message = self.queue_doc.message
		if not message:
			return ""

		message = message.replace(self.message_placeholder('tracker'), self.get_tracker_str())
		message = message.replace(self.message_placeholder('unsubscribe_url'),
			self.get_unsubscribe_str(recipient_email))
		message = message.replace(self.message_placeholder('cc'), self.get_receivers_str())
		message = message.replace(self.message_placeholder('recipient'),
			self.get_receipient_str(recipient_email))
		message = self.include_attachments(message)
		return message

	def get_tracker_str(self):
		tracker_url_html = \
			'<img src="https://{}/api/method/frappe.core.doctype.communication.email.mark_email_as_seen?name={}"/>'

		message = ''
		if frappe.conf.use_ssl and self.email_account_doc.track_email_status:
			message = quopri.encodestring(
				tracker_url_html.format(frappe.local.site, self.queue_doc.communication).encode()
			).decode()
		return message

	def get_unsubscribe_str(self, recipient_email):
		unsubscribe_url = ''
		if self.queue_doc.add_unsubscribe_link and self.queue_doc.reference_doctype:
			doctype, doc_name = self.queue_doc.reference_doctype, self.queue_doc.reference_name
			unsubscribe_url = get_unsubcribed_url(doctype, doc_name, recipient_email,
				self.queue_doc.unsubscribe_method, self.queue_doc.unsubscribe_param)

		return quopri.encodestring(unsubscribe_url.encode()).decode()

	def get_receivers_str(self):
		message = ''
		if self.queue_doc.expose_recipients == "footer":
			to_str = ', '.join(self.queue_doc.to)
			cc_str = ', '.join(self.queue_doc.cc)
			message = f"This email was sent to {to_str}"
			message = message + f" and copied to {cc_str}" if cc_str else message
		return message

	def get_receipient_str(self, recipient_email):
		message = ''
		if self.queue_doc.expose_recipients != "header":
			message = recipient_email
		return message

	def include_attachments(self, message):
		message_obj = self.get_message_object(message)
		attachments = self.queue_doc.attachments_list

		for attachment in attachments:
			if attachment.get('fcontent'):
				continue

			fid = attachment.get("fid")
			if fid:
				_file = frappe.get_doc("File", fid)
				fcontent = _file.get_content()
				attachment.update({
					'fname': _file.file_name,
					'fcontent': fcontent,
					'parent': message_obj
				})
				attachment.pop("fid", None)
				add_attachment(**attachment)

			elif attachment.get("print_format_attachment") == 1:
				attachment.pop("print_format_attachment", None)
				print_format_file = frappe.attach_print(**attachment)
				print_format_file.update({"parent": message_obj})
				add_attachment(**print_format_file)

		return safe_encode(message_obj.as_string())

@frappe.whitelist()
def retry_sending(name):
	doc = frappe.get_doc("Email Queue", name)
	if doc and (doc.status == "Error" or doc.status == "Partially Errored"):
		doc.status = "Not Sent"
		for d in doc.recipients:
			if d.status != 'Sent':
				d.status = 'Not Sent'
		doc.save(ignore_permissions=True)

@frappe.whitelist()
def send_now(name):
	record = EmailQueue.find(name)
	if record:
		record.send()

def on_doctype_update():
	"""Add index in `tabCommunication` for `(reference_doctype, reference_name)`"""
	frappe.db.add_index('Email Queue', ('status', 'send_after', 'priority', 'creation'), 'index_bulk_flush')

class QueueBuilder:
	"""Builds Email Queue from the given data
	"""
	def __init__(self, recipients=None, sender=None, subject=None, message=None,
			text_content=None, reference_doctype=None, reference_name=None,
			unsubscribe_method=None, unsubscribe_params=None, unsubscribe_message=None,
			attachments=None, reply_to=None, cc=None, bcc=None, message_id=None, in_reply_to=None,
			send_after=None, expose_recipients=None, send_priority=1, communication=None,
			read_receipt=None, queue_separately=False, is_notification=False,
			add_unsubscribe_link=1, inline_images=None, header=None,
			print_letterhead=False, with_container=False):
		"""Add email to sending queue (Email Queue)

		:param recipients: List of recipients.
		:param sender: Email sender.
		:param subject: Email subject.
		:param message: Email message.
		:param text_content: Text version of email message.
		:param reference_doctype: Reference DocType of caller document.
		:param reference_name: Reference name of caller document.
		:param send_priority: Priority for Email Queue, default 1.
		:param unsubscribe_method: URL method for unsubscribe. Default is `/api/method/frappe.email.queue.unsubscribe`.
		:param unsubscribe_params: additional params for unsubscribed links. default are name, doctype, email
		:param attachments: Attachments to be sent.
		:param reply_to: Reply to be captured here (default inbox)
		:param in_reply_to: Used to send the Message-Id of a received email back as In-Reply-To.
		:param send_after: Send this email after the given datetime. If value is in integer, then `send_after` will be the automatically set to no of days from current date.
		:param communication: Communication link to be set in Email Queue record
		:param queue_separately: Queue each email separately
		:param is_notification: Marks email as notification so will not trigger notifications from system
		:param add_unsubscribe_link: Send unsubscribe link in the footer of the Email, default 1.
		:param inline_images: List of inline images as {"filename", "filecontent"}. All src properties will be replaced with random Content-Id
		:param header: Append header in email (boolean)
		:param with_container: Wraps email inside styled container
		"""

		self._unsubscribe_method = unsubscribe_method
		self._recipients = recipients
		self._cc = cc
		self._bcc = bcc
		self._send_after = send_after
		self._sender = sender
		self._text_content = text_content
		self._message = message
		self._add_unsubscribe_link = add_unsubscribe_link
		self._unsubscribe_message = unsubscribe_message
		self._attachments = attachments

		self._unsubscribed_user_emails = None
		self._email_account = None

		self.unsubscribe_params = unsubscribe_params
		self.subject = subject
		self.reference_doctype = reference_doctype
		self.reference_name = reference_name
		self.expose_recipients = expose_recipients
		self.with_container = with_container
		self.header = header
		self.reply_to = reply_to
		self.message_id = message_id
		self.in_reply_to = in_reply_to
		self.send_priority = send_priority
		self.communication = communication
		self.read_receipt = read_receipt
		self.queue_separately = queue_separately
		self.is_notification = is_notification
		self.inline_images = inline_images
		self.print_letterhead = print_letterhead

	@property
	def unsubscribe_method(self):
		return self._unsubscribe_method or '/api/method/frappe.email.queue.unsubscribe'

	def _get_emails_list(self, emails=None):
		emails = split_emails(emails) if isinstance(emails, str) else (emails or [])
		return [each for each in set(emails) if each]

	@property
	def recipients(self):
		return self._get_emails_list(self._recipients)

	@property
	def cc(self):
		return self._get_emails_list(self._cc)

	@property
	def bcc(self):
		return self._get_emails_list(self._bcc)

	@property
	def send_after(self):
		if isinstance(self._send_after, int):
			return add_days(nowdate(), self._send_after)
		return self._send_after

	@property
	def sender(self):
		if not self._sender or self._sender == "Administrator":
			email_account = self.get_outgoing_email_account()
			return email_account.default_sender
		return self._sender

	def email_text_content(self):
		unsubscribe_msg = self.unsubscribe_message()
		unsubscribe_text_message = (unsubscribe_msg and unsubscribe_msg.text) or ''

		if self._text_content:
			return self._text_content + unsubscribe_text_message

		try:
			text_content = html2text(self._message)
		except HTMLParser.HTMLParseError:
			text_content = "See html attachment"
		return text_content + unsubscribe_text_message

	def email_html_content(self):
		email_account = self.get_outgoing_email_account()
		return get_formatted_html(self.subject, self._message, header=self.header,
			email_account=email_account, unsubscribe_link=self.unsubscribe_message(),
			with_container=self.with_container)

	def should_include_unsubscribe_link(self):
		return (self._add_unsubscribe_link == 1
			and self.reference_doctype
			and (self._unsubscribe_message or self.reference_doctype=="Newsletter"))

	def unsubscribe_message(self):
		if self.should_include_unsubscribe_link():
			return get_unsubscribe_message(self._unsubscribe_message, self.expose_recipients)

	def get_outgoing_email_account(self):
		if self._email_account:
			return self._email_account

		self._email_account = EmailAccount.find_outgoing(
			match_by_doctype=self.reference_doctype, match_by_email=self._sender, _raise_error=True)
		return self._email_account

	def get_unsubscribed_user_emails(self):
		if self._unsubscribed_user_emails is not None:
			return self._unsubscribed_user_emails

		all_ids = tuple(set(self.recipients + self.cc))

		unsubscribed = frappe.db.sql_list('''
			SELECT
				distinct email
			from
				`tabEmail Unsubscribe`
			where
				email in %(all_ids)s
				and (
					(
						reference_doctype = %(reference_doctype)s
						and reference_name = %(reference_name)s
					)
					or global_unsubscribe = 1
				)
		''', {
			'all_ids': all_ids,
			'reference_doctype': self.reference_doctype,
			'reference_name': self.reference_name,
		})

		self._unsubscribed_user_emails = unsubscribed or []
		return self._unsubscribed_user_emails

	def final_recipients(self):
		unsubscribed_emails = self.get_unsubscribed_user_emails()
		return [mail_id for mail_id in self.recipients if mail_id not in unsubscribed_emails]

	def final_cc(self):
		unsubscribed_emails = self.get_unsubscribed_user_emails()
		return [mail_id for mail_id in self.cc if mail_id not in unsubscribed_emails]

	def get_attachments(self):
		attachments = []
		if self._attachments:
			# store attachments with fid or print format details, to be attached on-demand later
			for att in self._attachments:
				if att.get('fid'):
					attachments.append(att)
				elif att.get("print_format_attachment") == 1:
					if not att.get('lang', None):
						att['lang'] = frappe.local.lang
					att['print_letterhead'] = self.print_letterhead
					attachments.append(att)
		return attachments

	def prepare_email_content(self):
		mail = get_email(recipients=self.final_recipients(),
			sender=self.sender,
			subject=self.subject,
			formatted=self.email_html_content(),
			text_content=self.email_text_content(),
			attachments=self._attachments,
			reply_to=self.reply_to,
			cc=self.final_cc(),
			bcc=self.bcc,
			email_account=self.get_outgoing_email_account(),
			expose_recipients=self.expose_recipients,
			inline_images=self.inline_images,
			header=self.header)

		mail.set_message_id(self.message_id, self.is_notification)
		if self.read_receipt:
			mail.msg_root["Disposition-Notification-To"] = self.sender
		if self.in_reply_to:
			mail.set_in_reply_to(self.in_reply_to)
		return mail

	def process(self, send_now=False):
		"""Build and return the email queues those are created.

		Sends email incase if it is requested to send now.
		"""
		final_recipients = self.final_recipients()
		queue_separately = (final_recipients and self.queue_separately) or len(final_recipients) > 20
		if not (final_recipients + self.final_cc()):
			return []

		email_queues = []
		queue_data = self.as_dict(include_recipients=False)
		if not queue_data:
			return []

		if not queue_separately:
			recipients = list(set(final_recipients + self.final_cc() + self.bcc))
			q = EmailQueue.new({**queue_data, **{'recipients': recipients}}, ignore_permissions=True)
			email_queues.append(q)
		else:
			for r in final_recipients:
				recipients = [r] if email_queues else list(set([r] + self.final_cc() + self.bcc))
				q = EmailQueue.new({**queue_data, **{'recipients': recipients}}, ignore_permissions=True)
				email_queues.append(q)

		if send_now:
			for doc in email_queues:
				doc.send()
		return email_queues

	def as_dict(self, include_recipients=True):
		email_account = self.get_outgoing_email_account()
		email_account_name = email_account and email_account.is_exists_in_db() and email_account.name

		mail = self.prepare_email_content()
		try:
			mail_to_string = cstr(mail.as_string())
		except frappe.InvalidEmailAddressError:
			# bad Email Address - don't add to queue
			frappe.log_error('Invalid Email ID Sender: {0}, Recipients: {1}, \nTraceback: {2} '
				.format(self.sender, ', '.join(self.final_recipients()), traceback.format_exc()),
				'Email Not Sent'
			)
			return

		d = {
			'priority': self.send_priority,
			'attachments': json.dumps(self.get_attachments()),
			'message_id': mail.msg_root["Message-Id"].strip(" <>"),
			'message': mail_to_string,
			'sender': self.sender,
			'reference_doctype': self.reference_doctype,
			'reference_name': self.reference_name,
			'add_unsubscribe_link': self._add_unsubscribe_link,
			'unsubscribe_method': self.unsubscribe_method,
			'unsubscribe_params': self.unsubscribe_params,
			'expose_recipients': self.expose_recipients,
			'communication': self.communication,
			'send_after': self.send_after,
			'show_as_cc': ",".join(self.final_cc()),
			'show_as_bcc': ','.join(self.bcc),
			'email_account': email_account_name or None
		}

		if include_recipients:
			d['recipients'] = self.final_recipients()

		return d
