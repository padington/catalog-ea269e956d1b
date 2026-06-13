"""One-time interactive Instagram login.

Run by a human: `python reels-catalog/login.py`
Prompts for credentials, handles 2FA, and saves a reusable session to
ig_session.json. The password is read with getpass and never stored or logged.
"""

import getpass
import os

SESSION_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ig_session.json")


def main():
    from instagrapi import Client

    username = input("Instagram username: ").strip()
    password = getpass.getpass("Instagram password: ")

    cl = Client()

    try:
        cl.login(username, password)
    except Exception as exc:
        # instagrapi raises a challenge/2FA exception; prompt for the code and retry.
        name = type(exc).__name__
        if "TwoFactor" in name or "Challenge" in name:
            code = input("Enter the 2FA / challenge code: ").strip()
            cl.login(username, password, verification_code=code)
        else:
            raise

    cl.dump_settings(SESSION_PATH)
    # Drop the password reference as soon as we are done with it.
    del password

    print(f"Login OK. Session saved to {SESSION_PATH}")
    print("You won't need to log in again until this session expires.")


if __name__ == "__main__":
    main()
