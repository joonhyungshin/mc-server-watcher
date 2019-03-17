from subprocess import Popen, PIPE
from threading import Thread
from urllib.request import urlopen, Request
import sys
import re
import json
import configparser
import os
import shlex
import time


def send_slack_message(hook_url, message):
    payload = {
        'text': message
    }
    data = json.dumps(payload).encode()
    req = Request(hook_url, data)
    urlopen(req)


class MCServerLog:
    server_log_re = re.compile(r'\[(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}):(?P<second>[0-9]{2})\] '
                               r'\[(?P<thread>.+)/(?P<level>(INFO|WARN|ERROR))\]: (?P<message>.*)')
    server_log_level = {
        'INFO': 0,
        'WARN': 1,
        'ERROR': 2,
        'UNKNOWN': 3
    }

    def __init__(self, message, source, server_log_re=None):
        if server_log_re:
            self.server_log_re = server_log_re
        self.source = source
        self.server_log = self.server_log_re.match(message)
        self.total_message = message
        self.message = message
        self.level = 'UNKNOWN'

        if self.server_log:
            for name in self.server_log.groupdict():
                self.__setattr__(name, self.server_log.group(name))

    def __bool__(self):
        return self.server_log is not None

    def __str__(self):
        return self.total_message

    def fit_level(self, filter_level):
        return self.server_log_level[self.level] >= self.server_log_level[filter_level]


class MCServer:
    def __init__(self, config, log_level='INFO'):
        self.exec_args = shlex.split(config['Minecraft']['RunServerCommand'])
        self.server_log_re = config['Minecraft']['ServerLogRegex']
        self.server_name = config['Minecraft']['ServerName']
        self.logout_cool_time = float(config['Minecraft']['LogoutCoolTime'])
        self.save_cool_time = float(config['Minecraft']['SaveCoolTime'])

        self.hook_url = config['SlackApp']['WebHookUrl']

        self.process = None

        self.stdout_reader = None
        self.stderr_reader = None
        self.log_level = log_level

        self.last_save = None
        self.user_set = {}

    def user_joined(self, user):
        if user not in self.user_set:
            self.user_set[user] = 0
        self.user_set[user] += 1

    def user_left(self, user):
        if user in self.user_set:
            self.user_set[user] -= 1
            if self.user_set[user] == 0:
                del self.user_set[user]

    def handle_log(self, server_log):
        join_re = r'(?P<name>.+) joined the game$'
        left_re = r'(?P<name>.+) left the game$'
        server_re = r'Starting Minecraft server on (?P<ip>.+):(?P<port>[0-9]+)'
        save_re = r'Saving.*'
        saved_re = r'Saved.*'
        done_re = r'Done.*'

        message = server_log.message

        if re.match(join_re, message):
            match = re.match(join_re, message)
            self.user_joined(match.group('name'))
            send_slack_message(self.hook_url, message + ' `{}`.'.format(self.server_name))

        elif re.match(left_re, message):
            match = re.match(left_re, message)
            self.user_left(match.group('name'))
            send_slack_message(self.hook_url, message + ' `{}`.'.format(self.server_name))

            if not self.user_set:
                if self.last_save is None or time.time() - self.last_save >= self.save_cool_time:
                    self.save_all()

        elif re.match(server_re, message):
            match = re.match(server_re, message)
            if not self.server_name:
                self.server_name = '{}:{}'.format(match.group('ip'), match.group('port'))

        elif re.match(done_re, message):
            send_slack_message(self.hook_url, 'Server `{}` opened.'.format(self.server_name))

        elif re.match(save_re, message) or re.match(saved_re, message):
            self.last_save = time.time()
            send_slack_message(self.hook_url, message + ' `{}`.'.format(self.server_name))

    def _read_stdout(self):
        fout = self.process.stdout
        while True:
            line = fout.readline()
            if line:
                server_log = MCServerLog(line, 'stdout', self.server_log_re)
                self.handle_log(server_log)
                if server_log.fit_level(self.log_level):
                    sys.stdout.write(line)

            else:
                break

    def _read_stderr(self):
        ferr = self.process.stderr
        while True:
            line = ferr.readline()
            if line:
                server_log = MCServerLog(line, 'stderr', self.server_log_re)
                self.handle_log(server_log)
                if server_log.fit_level(self.log_level):
                    sys.stderr.write(line)

            else:
                break

    def start(self):
        self.process = Popen(args=self.exec_args,
                             bufsize=1, stdin=PIPE, stdout=PIPE, stderr=PIPE, universal_newlines=True)
        self.stdout_reader = Thread(target=self._read_stdout)
        self.stderr_reader = Thread(target=self._read_stderr)

        self.stdout_reader.start()
        self.stderr_reader.start()

    def stop(self):
        self.process.terminate()

    def kill(self):
        self.process.kill()

    def send_message(self, message):
        fin = self.process.stdin

        if not message or message[-1] != '\n':
            message += '\n'
        try:
            return fin.write(message)
        except (BrokenPipeError, OSError):
            return -1

    def save_all(self):
        self.send_message('save-all flush')

    def is_running(self):
        return self.process.poll() is None

    def exit_code(self):
        return self.process.returncode


def handle(server_log, config):
    join_re = r'(?P<name>.+) joined the game$'
    left_re = r'(?P<name>.+) left the game$'
    server_re = r'Starting Minecraft server on (?P<ip>.+):(?P<port>[0-9]+)'
    done_re = r'Done.*'

    message = server_log.message
    hook_url = config['SlackApp']['WebhookUrl']

    global server_name

    if re.match(join_re, message) or re.match(left_re, message):
        send_slack_message(hook_url, message + ' {}.'.format(server_name))
    elif re.match(server_re, message):
        match = re.match(server_re, message)
        server_name = '{}:{}'.format(match.group('ip'), match.group('port'))
    elif re.match(done_re, message):
        send_slack_message(hook_url, 'Server {} opened.'.format(server_name))


def main():
    config = configparser.ConfigParser()
    config.read('config.ini')
    os.chdir(config['Minecraft']['GameDirectory'])
    server = MCServer(config)
    server.start()
    while server.is_running():
        server.send_message(input())

    send_slack_message(server.hook_url, 'Server {} closed.'.format(server.server_name))


if __name__ == '__main__':
    main()
