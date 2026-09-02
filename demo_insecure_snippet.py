"""DEMO ONLY — deliberately insecure code to exercise Datadog SAST.

This module is never imported or executed anywhere in the app. It exists purely
so the Datadog Static Analysis (python-security ruleset) has known-bad patterns
to flag on this pull request, demonstrating the Code Security PR-comment flow.
Delete this file (and the demo branch) after the demo.
"""

import hashlib
import pickle
import subprocess


def weak_hash(data: str) -> str:
    # python-security: MD5 is a broken hash for security use.
    return hashlib.md5(data.encode()).hexdigest()


def run_user_command(user_input: str):
    # python-security: shell=True with untrusted input -> command injection.
    return subprocess.call("echo " + user_input, shell=True)


def load_untrusted(blob: bytes):
    # python-security: pickle.loads on untrusted data -> arbitrary code execution.
    return pickle.loads(blob)


def evaluate(expr: str):
    # python-security: eval on dynamic input -> code injection.
    return eval(expr)
