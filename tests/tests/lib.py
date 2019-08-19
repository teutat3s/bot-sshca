from contextlib import contextmanager
import hashlib
import os
import signal
import subprocess
import time
from typing import List, Set

import requests

def getDefaultExpectedHash() -> bytes:
    # "uniquestring" is stored in /etc/unique of the SSH server. We then run the command `sha1sum /etc/unique` via kssh
    # and assert that the output contains the sha1 hash of uniquestring. This checks to make sure the command given to
    # kssh is actually executing on the remote server.
    return hashlib.sha1(b"uniquestring").hexdigest().encode('utf-8')

class TestConfig:
    # Not actually a test class so mark it to be skipped
    __test__ = False

    def __init__(self, subteam, subteam_secondary, username, bot_username, expected_hash, subteams):
        self.subteam: str = subteam
        self.subteam_secondary: str = subteam_secondary
        self.username: str = username
        self.bot_username: str = bot_username
        self.expected_hash: bytes = expected_hash
        self.subteams: List[str] = subteams

    @staticmethod
    def getDefaultTestConfig():
        return TestConfig(
            os.environ['SUBTEAM'],
            os.environ['SUBTEAM_SECONDARY'],
            os.environ['KSSH_USERNAME'],
            os.environ['BOT_USERNAME'],
            getDefaultExpectedHash(),
            [os.environ['SUBTEAM'] + postfix for postfix in [".ssh.prod", ".ssh.staging", ".ssh.root_everywhere"]]
        )

def run_command_with_agent(cmd: str) -> bytes:
    """
    Run the given command in a shell session with a running ssh-agent
    :param cmd:     The command to run
    :return:        The stdout of the process
    """
    return run_command("eval `ssh-agent` && " + cmd)

def run_command(cmd: str, timeout: int=15) -> bytes:
    """
    Run the given command in a shell with the given timeout
    :param cmd:         The command to run
    :param timeout:     The timeout in seconds
    :return:            The stdout of the process
    """
    # In order to properly run a command with a timeout and shell=True, we use Popen with a shell and group all child
    # processes so we can kill all of them. See:
    # - https://stackoverflow.com/questions/36952245/subprocess-timeout-failure
    # - https://stackoverflow.com/questions/4789837/how-to-terminate-a-python-subprocess-launched-with-shell-true
    with subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, preexec_fn=os.setsid) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            if process.returncode != 0:
                print(f"Output before return: {repr(stdout)}, {repr(stderr)}")
                raise subprocess.CalledProcessError(process.returncode, cmd, stdout, stderr)
            return stdout
        except subprocess.TimeoutExpired as e:
            os.killpg(process.pid, signal.SIGINT)
            print(f"Output before timeout: {process.communicate()[0]}")
            raise e

def read_file(filename: str) -> List[bytes]:
    """
    Read the contents of the given filename to a list of strings. If it is a normal file,
    uses the standard open() function. Otherwise, uses `keybase fs read`. This is because
    fuse is not running in the container so a normal open call will not work for KBFS.
    :param filename:    The name of the file to read
    :return:            A list of lines in the file
    """
    if filename.startswith("/keybase/"):
        return run_command(f"keybase fs read {filename}").splitlines()
    with open(filename, 'rb') as f:
        return f.readlines()

def clear_keys():
    # Clear all keys generated by kssh
    try:
        run_command("rm -rf ~/.ssh/keybase-signed-key*")
    except subprocess.CalledProcessError:
        pass

def clear_local_config():
    # Clear kssh's local config file
    try:
        run_command("rm -rf ~/.ssh/kssh-config.json")
    except subprocess.CalledProcessError:
        pass

def load_env(filename: str):
    # Load the environment based off of the given filename which is the path to the python test script
    env_name = os.path.basename(filename).split(".")[0]
    return requests.get(f"http://ca-bot:8080/load_env?filename={env_name}").content == b"OK"

def assert_contains_hash(expected_hash: bytes, output: bytes):
    assert expected_hash in output

@contextmanager
def simulate_two_teams(tc: TestConfig):
    # A context manager that simulates running the given function in an environment with two teams set up
    run_command(f"keybase fs read /keybase/team/{tc.subteam}.ssh.staging/kssh-client.config | "
                     f"sed 's/{tc.subteam}.ssh.staging/{tc.subteam_secondary}/g' | "
                     f"sed 's/{tc.bot_username}/otherbotname/g' | "
                     f"keybase fs write /keybase/team/{tc.subteam_secondary}/kssh-client.config")
    try:
        yield
    finally:
        run_command(f"keybase fs rm /keybase/team/{tc.subteam_secondary}/kssh-client.config")

@contextmanager
def outputs_audit_log(tc: TestConfig, filename: str, expected_number: int):
    # A context manager that asserts that the given function triggers expected_number of audit logs to be added to the
    # log at the given filename

    # Make a set of the lines in the audit log before we ran
    before_lines = set(read_file(filename))

    # Then run the code inside the context manager
    yield

    # And sleep to give KBFS some time
    time.sleep(1.5)

    # Then see if there are new lines using set difference. This is only safe/reasonable since we include a
    # timestamp in audit log lines.
    after_lines = set(read_file(filename))
    new_lines = after_lines - before_lines

    cnt = 0
    for line in new_lines:
        line = line.decode('utf-8')
        if line and f"Processing SignatureRequest from user={tc.username}" in line and f"principals:{tc.subteam}.ssh.staging,{tc.subteam}.ssh.root_everywhere, expiration:+1h, pubkey:ssh-ed25519" in line:
            cnt += 1

    if cnt != expected_number:
        assert False, f"Found {cnt} audit log entries, expected {expected_number}! New audit logs: {new_lines}"

def get_principals(certificateFilename: str) -> Set[str]:
    inPrincipalsSection = False
    principalsIndentationLevel = 16
    foundPrincipals: Set[str] = set()
    for line in run_command(f"cat {certificateFilename} | ssh-keygen -L -f /dev/stdin").splitlines():
        if line.strip().startswith(b"Principals:"):
            inPrincipalsSection = True
            continue
        if len(line) - len(line.lstrip()) != principalsIndentationLevel:
            inPrincipalsSection = False
        if inPrincipalsSection:
            foundPrincipals.add(line.strip().decode('utf-8'))
    return foundPrincipals
