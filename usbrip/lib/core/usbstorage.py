#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""LICENSE

Copyright (C) 2018 Sam Freeside

This file is part of usbrip.

usbrip is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

usbrip is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with usbrip.  If not, see <http://www.gnu.org/licenses/>.
"""

__author__ = 'Sam Freeside (@snovvcrash)'
__email__  = 'snovvcrash@protonmail[.]ch'
__site__   = 'https://github.com/snovvcrash/usbrip'
__brief__  = 'USB Storage handler.'

import re
import json
import subprocess
import os
from base64 import b64encode
from datetime import datetime

import usbrip.lib.core.config as cfg
from usbrip.lib.core.usbevents import USBEvents
from usbrip.lib.core.usbevents import _filter_events
from usbrip.lib.core.usbevents import _dump_events
from usbrip.lib.core.usbevents import _process_auth_list
from usbrip.lib.core.common import MONTH_ENUM
from usbrip.lib.core.common import is_correct
from usbrip.lib.core.common import print_info
from usbrip.lib.core.common import print_warning
from usbrip.lib.core.common import print_critical
from usbrip.lib.core.common import print_secret
from usbrip.lib.core.common import USBRipError
from usbrip.lib.utils.debug import time_it
from usbrip.lib.utils.debug import time_it_if_debug


# ----------------------------------------------------------
# ---------------------- USB Storage -----------------------
# ----------------------------------------------------------


class USBStorage:

	_STORAGE_BASE = '/var/opt/usbrip/storage'

	_7Z_WRONG_PASSWORD_ERROR = -1
	_7Z_PERMISSION_ERROR     = -2
	_7Z_UNKNOWN_ERROR        = -3

	# -------------------- USB Storage List --------------------

	@staticmethod
	@time_it_if_debug(cfg.DEBUG, time_it)
	def list_storage(storage_type, password):
		storage_full_path = f'{USBStorage._STORAGE_BASE}/{storage_type}.7z'
		if not os.path.isfile(storage_full_path):
			print_critical(f'Storage not found: "{storage_full_path}"')
			return

		try:
			out = _7zip_list(storage_full_path, password)
		except USBRipError as e:
			print_critical(str(e), errcode=e.errors['errcode'], initial_error=e.errors['initial_error'])
			return

		if '--' in out:
			print(out[out.index('--'):] + '--')
		else:
			print_critical('Undefined behaviour while listing storage contents', initial_error=out)

	# -------------------- USB Storage Open --------------------

	@staticmethod
	@time_it_if_debug(cfg.DEBUG, time_it)
	def open_storage(storage_type, password, columns, *, sieve=None, repres=None):
		storage_full_path = f'{USBStorage._STORAGE_BASE}/{storage_type}.7z'
		if not os.path.isfile(storage_full_path):
			print_critical(f'Storage not found: "{storage_full_path}"')
			return

		try:
			out = _7zip_unpack(storage_full_path, password)
		except USBRipError as e:
			print_critical(str(e), errcode=e.errors['errcode'], initial_error=e.errors['initial_error'])
			return

		if 'Everything is Ok' in out:
			base_filename = re.search(r'Extracting\s*(.*?$)', out, re.MULTILINE).group(1)
			json_file = f'{USBStorage._STORAGE_BASE}/{base_filename}'
			USBEvents.open_dump(json_file, columns, sieve=sieve, repres=repres)
			os.remove(json_file)
		else:
			print_critical('Undefined behaviour while unpacking storage', initial_error=out)

	# ------------------- USB Storage Update -------------------

	@staticmethod
	@time_it_if_debug(cfg.DEBUG, time_it)
	def update_storage(
		storage_type,
		password=None,
		*,
		input_auth=None,
		attributes=None,
		compression_level='5',
		indent=4,
		sieve=None
	):
		if storage_type == 'history':
			events_to_show = _get_history_events(sieve)
		elif storage_type == 'violations':
			try:
				events_to_show = _get_violation_events(sieve, input_auth, attributes, indent)
			except USBRipError as e:
				print_critical(str(e), initial_error=e.errors['initial_error'])
				return 1

		if events_to_show is None:
			return 1

		if events_to_show:
			min_date, max_date = _get_dates(events_to_show)
		else:
			print_info('No events to append')
			return 1

		storage_full_path = f'{USBStorage._STORAGE_BASE}/{storage_type}.7z'
		if not os.path.isfile(storage_full_path):
			print_critical(f'Storage not found: "{storage_full_path}"')
			return 1

		print_info(f'Updating storage: "{storage_full_path}"')

		try:
			out = _7zip_unpack(storage_full_path, password)
		except USBRipError as e:
			print_critical(str(e), errcode=e.errors['errcode'], initial_error=e.errors['initial_error'])
			return 1

		if 'Everything is Ok' in out:
			os.remove(storage_full_path)
			base_filename = re.search(r'Extracting\s*(.*?$)', out, re.MULTILINE).group(1)
			json_file = f'{USBStorage._STORAGE_BASE}/{base_filename}'

			with open(json_file, 'r', encoding='utf-8') as dump:
				events_dumped = json.load(dump)
			os.remove(json_file)

			merged_events = _merge_json_events(events_dumped, events_to_show)

			if len(base_filename) > 9:  # len('mmdd.json') == 9
				min_date = base_filename[:4]

			new_json_file = f'{USBStorage._STORAGE_BASE}/{min_date}-{max_date}.json'
			_dump_events(merged_events, storage_type, new_json_file, indent)

			try:
				out = _7zip_pack(storage_full_path, new_json_file, password, compression_level)
			except USBRipError as e:
				os.remove(new_json_file)
				print_critical(str(e), errcode=e.errors['errcode'], initial_error=e.errors['initial_error'])
				return 1

			if 'Everything is Ok' in out:
				print_info('Storage was successfully updated')
			else:
				print_critical('Undefined behaviour while creating storage', initial_error=out)

			os.remove(new_json_file)

		else:
			print_critical('Undefined behaviour while unpacking storage', initial_error=out)

	# ------------------- USB Storage Create -------------------

	@staticmethod
	@time_it_if_debug(cfg.DEBUG, time_it)
	def create_storage(
		storage_type,
		*,
		password=None,
		input_auth=None,
		attributes=None,
		compression_level='5',
		indent=4,
		sieve=None
	):
		if storage_type == 'history':
			events_to_show = _get_history_events(sieve)
		elif storage_type == 'violations':
			try:
				events_to_show = _get_violation_events(sieve, input_auth, attributes, indent)
			except USBRipError as e:
				print_critical(str(e), initial_error=e.errors['initial_error'])
				return 1

		if events_to_show is None:
			return 1

		if events_to_show:
			min_date, max_date = _get_dates(events_to_show)
			json_file = f'{USBStorage._STORAGE_BASE}/{min_date}-{max_date}.json'
		else:
			json_file = f'{USBStorage._STORAGE_BASE}/{datetime.now().strftime("%m%d")}.json'

		try:
			_dump_events(events_to_show, storage_type, json_file, indent)
		except USBRipError as e:
			print_critical(str(e), initial_error=e.errors['initial_error'])
			return 1

		if password is None:
			print_warning('No password provided, generating random one')
			password = _gen_random_password(12)

		storage_full_path = f'{USBStorage._STORAGE_BASE}/{storage_type}.7z'
		if os.path.exists(storage_full_path):
			os.remove(storage_full_path)

		try:
			out = _7zip_pack(storage_full_path, json_file, password, compression_level)
		except USBRipError as e:
			os.remove(json_file)
			print_critical(str(e), errcode=e.errors['errcode'], initial_error=e.errors['initial_error'])
			return 1

		if 'Everything is Ok' in out:
			print_info(f'New {storage_type} storage: "{storage_full_path}"')
			print_secret('Your password is', secret=password)
			os.remove(json_file)
		else:
			print_critical('Undefined behaviour while creating storage', initial_error=out)

	# ------------------- USB Storage Passwd -------------------

	@staticmethod
	@time_it_if_debug(cfg.DEBUG, time_it)
	def change_password(storage_type, old_password, new_password, *, compression_level='5'):
		storage_full_path = f'{USBStorage._STORAGE_BASE}/{storage_type}.7z'
		if not os.path.isfile(storage_full_path):
			print_critical(f'Storage not found: "{storage_full_path}"')
			return

		try:
			out = _7zip_unpack(storage_full_path, old_password)
			if 'Everything is Ok' in out:
				os.remove(storage_full_path)

				base_filename = re.search(r'Extracting\s*(.*?$)', out, re.MULTILINE).group(1)
				json_file = f'{USBStorage._STORAGE_BASE}/{base_filename}'

				out = _7zip_pack(storage_full_path, json_file, new_password, compression_level)
				if 'Everything is Ok' in out:
					print_info('Password was successfully changed')
				else:
					print_critical('Undefined behaviour while creating storage', initial_error=out)

				os.remove(json_file)

			else:
				print_critical('Undefined behaviour while unpacking storage', initial_error=out)

		except USBRipError as e:
			print_critical(str(e), errcode=e.errors['errcode'], initial_error=e.errors['initial_error'])
			return


# ----------------------------------------------------------
# ----------------------- Utilities ------------------------
# ----------------------------------------------------------


def _gen_random_password(length):
	while True:
		b64 = b64encode(os.urandom(length)).decode('utf-8')
		password = re.sub(r'[+=/]', '', b64)[:length]
		if is_correct(password):
			return password


def _get_history_events(sieve):
	ue = USBEvents()
	if not ue:
		return None

	return _filter_events(ue._all_events, sieve)


def _get_violation_events(sieve, input_auth, attributes, indent):
	try:
		auth = _process_auth_list(input_auth, indent)
	except json.decoder.JSONDecodeError as e:
		raise USBRipError(
			'Failed to decode authorized device list (JSON)',
			errors={'initial_error': str(e)}
		)

	if not attributes:
		attributes = auth.keys()

	ue = USBEvents()
	if not ue:
		return None

	for event in ue._all_events:
		try:
			if any(event[key] not in vals and
                   event[key] is not None
                   for key, vals in zip(attributes, auth.values())):
				ue._violations.append(event)
		except KeyError as e:
			raise USBRipError(
				'No such attribute in authorized device list',
				errors={'initial_error': str(e)}
			)

	return _filter_events(ue._violations, sieve)


def _get_dates(events_to_show):
	dates = {event['conn'][:6] for event in events_to_show}
	min_date = min(dates, key=lambda i: MONTH_ENUM[i[:3]] + i[3:]).split()
	min_date = MONTH_ENUM[min_date[0]].zfill(2) + min_date[-1].zfill(2)
	max_date = max(dates, key=lambda i: MONTH_ENUM[i[:3]] + i[3:]).split()
	max_date = MONTH_ENUM[max_date[0]].zfill(2) + max_date[-1].zfill(2)

	return (min_date, max_date)


'''
def _create_shadow(password, rounds):
	from bcrypt import hashpw, gensalt
	hashed = hashpw(password.encode('utf-8'), gensalt(rounds))
	with open('/var/opt/usbrip/shadow', 'wb') as f:
		f.write(hashed)
'''


def _merge_json_events(events_dumped, events_to_show):
	events_dumped_set = {json.dumps(event) for event in events_dumped}
	events_union_set = events_dumped_set.union([json.dumps(event) for event in events_to_show])
	events_union = [json.loads(event) for event in events_union_set]
	events_union_sorted = sorted(events_union, key=lambda i: MONTH_ENUM[i['conn'][:3]] + i['conn'][3:])

	return events_union_sorted


def _7zip_list(archive, password):
	print_info(f'Listing archive: "{archive}"')

	cmd = [
		'7z',
		'l',
		archive,
		'-p' + password
	]

	out, errcode, errmsg, e = _7zip_subprocess_handler(cmd)
	if errcode:
		raise USBRipError(errmsg, errors={'errcode': errcode, 'initial_error': e})

	return out


def _7zip_unpack(archive, password):
	print_info(f'Unpacking archive: "{archive}"')

	cmd = [
		'7z',
		'e',
		archive,
		'-p' + password,
		'-o' + USBStorage._STORAGE_BASE,
		'-y'
	]

	out, errcode, errmsg, e = _7zip_subprocess_handler(cmd)
	if errcode:
		raise USBRipError(errmsg, errors={'errcode': errcode, 'initial_error': e})

	return out


def _7zip_pack(archive, file, password, compression_level):
	print_info(f'Creating storage (7-Zip): "{archive}"')

	cmd = [
		'7z',
		'a',
		archive,
		file,
		'-mhe=on',
		'-p' + password,
		'-mx=' + compression_level
	]

	out, errcode, errmsg, e = _7zip_subprocess_handler(cmd)
	if errcode:
		raise USBRipError(errmsg, errors={'errcode': errcode, 'initial_error': e})

	return out


def _7zip_subprocess_handler(cmd):
	try:
		out = subprocess.check_output(cmd).decode('utf-8')
	except subprocess.CalledProcessError as e:
		initial_error = e.output.decode('utf-8')

		if 'Wrong password?' in initial_error:
			errmsg = 'Can not open encrypted archive. Wrong password?'
			errcode = USBStorage._7Z_WRONG_PASSWORD_ERROR
		elif 'can not open output file' in initial_error:
			errmsg = 'Permission denied. Retry with sudo'
			errcode = USBStorage._7Z_PERMISSION_ERROR
		else:
			errmsg = 'Something went wrong while working with 7-Zip archive'
			errcode = USBStorage._7Z_UNKNOWN_ERROR

		return ('', errcode, errmsg, initial_error)

	return (out, 0, '', '')
