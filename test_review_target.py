
import os
import subprocess

def run_command(user_input):
    """Run a shell command - has security issues for review detection."""
    # BUG: Command injection vulnerability
    result = subprocess.run(user_input, shell=True, capture_output=True)
    return result.stdout.decode()

def get_api_key():
    """Has a hardcoded secret."""
    # ISSUE: Hardcoded API key
    api_key = "sk-1234567890abcdef"
    return api_key

def divide_numbers(a, b):
    """Has a zero division bug."""
    # BUG: No zero check
    return a / b

def read_file(path):
    """Has path traversal risk."""
    # ISSUE: No path validation
    with open(path, 'r') as f:
        return f.read()

class UserService:
    def __init__(self):
        self.users = []
    
    def get_user(self, user_id):
        # BUG: O(n) lookup instead of dict
        for u in self.users:
            if u["id"] == user_id:
                return u
        return None
