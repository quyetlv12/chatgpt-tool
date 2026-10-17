"""Discover supported local Chromium browsers; never accept an executable from a request."""
import os
import shutil
import sys


BROWSER_NAMES = {'auto': 'Tự động (Chrome / Chromium)', 'brave': 'Brave',
                 'chrome': 'Google Chrome', 'edge': 'Microsoft Edge', 'chromium': 'Chromium'}


def browser_executable(browser_id):
    if sys.platform == 'darwin':
        apps = {'brave': ('Brave Browser', 'Brave Browser'), 'chrome': ('Google Chrome', 'Google Chrome'),
                'edge': ('Microsoft Edge', 'Microsoft Edge'), 'chromium': ('Chromium', 'Chromium')}
        app, binary = apps[browser_id]
        candidates = [os.path.join(base, app + '.app', 'Contents', 'MacOS', binary)
                      for base in ('/Applications', os.path.expanduser('~/Applications'))]
    elif sys.platform == 'win32':
        relative = {'brave': ('BraveSoftware', 'Brave-Browser', 'Application', 'brave.exe'),
                    'chrome': ('Google', 'Chrome', 'Application', 'chrome.exe'),
                    'edge': ('Microsoft', 'Edge', 'Application', 'msedge.exe'),
                    'chromium': ('Chromium', 'Application', 'chrome.exe')}[browser_id]
        candidates = [os.path.join(base, *relative) for key in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA')
                      if (base := os.environ.get(key))]
    else:
        commands = {'brave': ('brave-browser', 'brave-browser-stable', 'brave'),
                    'chrome': ('google-chrome', 'google-chrome-stable'),
                    'edge': ('microsoft-edge', 'microsoft-edge-stable'),
                    'chromium': ('chromium', 'chromium-browser')}[browser_id]
        candidates = [path for command in commands if (path := shutil.which(command))]
    return next((path for path in candidates if os.path.isfile(path) and os.access(path, os.X_OK)), None)


def available_browsers():
    return [{'id': key, 'name': name} for key, name in BROWSER_NAMES.items()
            if key == 'auto' or browser_executable(key)]


def browser_launch_options(browser_id):
    if not isinstance(browser_id, str) or browser_id not in BROWSER_NAMES:
        raise ValueError('Trình duyệt không được hỗ trợ')
    if browser_id == 'auto':
        return {'channel': 'chrome'}
    path = browser_executable(browser_id)
    if not path:
        raise ValueError('Không tìm thấy {} trên máy; hãy cài hoặc chọn lại trình duyệt'.format(BROWSER_NAMES[browser_id]))
    return {'executable_path': path}
