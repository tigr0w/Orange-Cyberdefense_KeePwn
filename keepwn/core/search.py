import concurrent.futures
from datetime import datetime
from itertools import repeat
from io import BytesIO

import pefile
from impacket.smbconnection import SessionError
from lxml import etree
from termcolor import colored

from keepwn.core.trigger import read_config_file
from keepwn.utils.logging import print_error_target, print_debug_target, format_path, print_success_target, print_error, print_info, display_smb_error
from keepwn.utils.parser import parse_mandatory_options, parse_search_integers
from keepwn.utils.smb import smb_connect

def search(options):
    targets, share, domain, user, password, lm_hash, nt_hash = parse_mandatory_options(options)
    threads, max_depth = parse_search_integers(options)

    if len(targets) == 1:
        search_target(targets[0], share, user, password, domain, lm_hash, nt_hash, max_depth)
    else:
        print_info("Starting remote KeePass search with {} threads".format(threads))
        with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
            executor.map(search_target, targets, repeat(share), repeat(user), repeat(password), repeat(domain), repeat(lm_hash), repeat(nt_hash), repeat(max_depth))


def search_target(target, share, user, password, domain, lm_hash, nt_hash, max_depth):
    # TODO: add option to check for running KeePass process through RPC, see https://github.com/fortra/impacket/blob/master/examples/tstool.py

    # admin connection to target
    smb_connection, error = smb_connect(target, share, user, password, domain, lm_hash, nt_hash)
    if error or not smb_connection:
        display_smb_error(error, target, True)
        return

    # search KeePass.exe global installation in default location
    keepass_exe = search_global_path(share, smb_connection)

    # whether we find a global install or not, we look for KeePass configuration file (and get LastUpdateTime at the same time)
    config_files = search_config_file(share, smb_connection)
    last_update_time = get_last_update_check(share, smb_connection, config_files)

    # if global KeePass installation was not found  but there was a configuration file, search for portable installations
    if config_files and not keepass_exe:
        keepass_exe = search_local_path(share, smb_connection, max_depth)

    # display results
    if keepass_exe:
        version = get_keepass_version(share, smb_connection, keepass_exe)
        display_message = get_found_display(share, keepass_exe, version, last_update_time)
        print_success_target(target, display_message)
        return
    elif config_files:  # edge case
        print_success_target(target, "Found keepass configuration files in {} but no KeePass.exe, you may increase --max-depth to find portable installation".format(format_path('%APPDATA%\Roaming\KeePass\KeePass.config.xml')))
        return

    print_debug_target(target, "No KeePass-related file found")


def search_global_path(share, smb_connection):
    try:
        path = '\\Program Files\\KeePass Password Safe 2\\KeePass.exe'
        for file in smb_connection.listPath(share, path):
            return path
    except SessionError as e:  # TODO: be more precise in error handling (ex: connection)
        return None


def search_local_path(share, smb_connection, max_depth):
    # TODO: search for multiple local KeePass.exe files ???
    starting_folder = '\\*'
    current_depth = 1
    return recursive_folder_search(share, smb_connection, starting_folder, current_depth, max_depth)


def search_config_file(share, smb_connection):
    config_paths = []
    try:
        for file in smb_connection.listPath(share, '\\Users\\*'):
            if file.is_directory():
                try:
                    path = '\\Users\\{}\\AppData\\Roaming\\KeePass\\KeePass.config.xml'.format(file.get_longname())
                    for file in smb_connection.listPath(share, path):
                        config_paths.append(path)
                except SessionError as e:
                    pass  # the file was not found
    except SessionError as e:
        pass  # the file was not found
    return config_paths


def get_last_update_check(share, smb_connection, config_files):
    last_update_checks = []
    if config_files:
        for config_file in config_files:
            try:
                global_config_file_content = read_config_file(smb_connection, share, config_files[0])
                tree = etree.fromstring(global_config_file_content.encode())
                update_check = False
                for configuration in tree.findall('./Application/Start/CheckForUpdate'):
                    if configuration.text == 'true':
                        update_check = True
                if update_check:
                    for configuration in tree.findall('./Application/LastUpdateCheck'):
                        last_update_checks.append(datetime.strptime(configuration.text, '%Y-%m-%dT%H:%M:%SZ'))
            except SessionError:
                pass # the file was not found
    return max(last_update_checks, default=None)


def get_keepass_version(share, smb_connection, path):
    version = None
    try:
        buffer = BytesIO()
        smb_connection.getFile(share, path, buffer.write)
        pe = pefile.PE(data=buffer.getvalue())
        enum_dict = pe.dump_dict()
        version = enum_dict['Version Information'][0][2][11][b'ProductVersion'].decode("utf-8")
    except SessionError as e:
        pass  # the file was not found
    return version


def get_found_display(share, path, version, last_update_check):
    if not version:
        version_message = 'Unkown'
    else:
        version_message = '.'.join(version.split('.')[0:3])

    if not last_update_check:
        last_update_check_message = 'Unkown'
    else:
        difference = datetime.utcnow() - last_update_check
        last_update_check_message = '{} days ago'.format(difference.days)
        if difference.days == 0:
            if difference.seconds // 3600 > 0:
                last_update_check_message = '{} hours ago'.format(difference.seconds // 3600)
            else:
                last_update_check_message = '{} minutes ago'.format((difference.seconds // 60) % 60)

    message = "Found {} ".format(format_path('\\\\{}{}'.format(share, path)))
    message += colored("(Version: ", "cyan")
    message += colored(version_message, "yellow") + ', '
    message += colored("LastUpdateCheck: ", "cyan")
    message += colored(last_update_check_message, "yellow")
    message += colored(")", "cyan")

    return message


def recursive_folder_search(share, smb_connection, current_path, current_depth, max_depth):
    # Base case: If the current depth exceeds the maximum depth, return None
    if current_depth > max_depth:
        return None

    try:
        # Check if the target folder exists in the current folder
        for file in smb_connection.listPath(share, current_path):
            # we exclude Program Files, Windows, ProgramData and AppData as a tradeoff for faster search, add option to choose?
            if current_path.startswith('\Program Files') or current_path.startswith('\Windows') or current_path.startswith('\ProgramData') or 'AppData' in current_path:
                return None
            # if a directory contains "keepass", looks for KeePass.exe inside
            if file.is_directory() and "keepass" in file.get_longname().lower():
                try:
                    for sub_file in smb_connection.listPath(share, current_path[:-1] + file.get_longname() + '\\KeePass.exe'):
                        return current_path[:-1] + file.get_longname() + '\\KeePass.exe'
                except SessionError as e:  # the file was not found
                    pass
            # launch new recursive search (excludes current and upper folders)
            if file.is_directory() and (file.get_longname() not in ['.', '..']):
                result = recursive_folder_search(share, smb_connection, current_path[:-1] + file.get_longname() + '\\*', current_depth + 1, max_depth)
                if result:
                    return result
    except SessionError as e:
        return None

    return None
