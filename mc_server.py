from subprocess import Popen, PIPE
from threading import Thread
from urllib.request import urlopen, Request
import sys
import re
import json
import os


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
    def __init__(self, exec_args, executor='java', server_log_re=None, handle=None, log_level='INFO'):
        self.exec_args = exec_args
        self.executor = executor
        if handle is None:
            self.handle = lambda x: None
        else:
            self.handle = handle
        self.process = None

        self.server_log_re = server_log_re

        self.stdout_reader = None
        self.stderr_reader = None
        self.log_level = log_level

    def _read_stdout(self):
        fout = self.process.stdout
        while True:
            line = fout.readline()
            if line:
                server_log = MCServerLog(line, 'stdout', self.server_log_re)
                self.handle(server_log)
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
                self.handle(server_log)
                if server_log.fit_level(self.log_level):
                    sys.stderr.write(line)

            else:
                break

    def start(self):
        self.process = Popen(args=[self.executor] + self.exec_args,
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
        except BrokenPipeError:
            return -1

    def is_running(self):
        return self.process.poll() is None

    def exit_code(self):
        return self.process.returncode


def send_slack_message(message, channel, username='Minecraft'):
    payload = {
        'channel': channel,
        'username': username,
        'text': message
    }
    data = json.dumps(payload).encode()
    req = Request(os.environ['SLACK_HOOK_URL'], data)
    urlopen(req)


server_name = ''


def handle(server_log):
    join_re = r'(?P<name>.+) joined the game$'
    left_re = r'(?P<name>.+) left the game$'
    server_re = r'Starting Minecraft server on (?P<ip>.+):(?P<port>[0-9]+)'
    done_re = r'Done.*'

    message = server_log.message

    global server_name

    if re.match(join_re, message) or re.match(left_re, message):
        send_slack_message(message + ' {}.'.format(server_name), '#minecraft')
    elif re.match(server_re, message):
        match = re.match(server_re, message)
        server_name = '{}:{}'.format(match.group('ip'), match.group('port'))
    elif re.match(done_re, message):
        send_slack_message('Server {} opened.'.format(server_name), '#minecraft')


def main():
    server = MCServer(sys.argv[1:], handle=handle)
    server.start()
    while server.is_running():
        server.send_message(input())

    global server_name
    send_slack_message('Server {} closed.'.format(server_name), '#minecraft')


if __name__ == '__main__':
    main()
