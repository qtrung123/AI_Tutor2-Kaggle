import argparse
import getpass

from backend.auth_store import claim_legacy_user


def main() -> None:
    parser = argparse.ArgumentParser(description="Assign credentials to the preserved legacy local_student account.")
    parser.add_argument("--email", required=True)
    parser.add_argument("--display-name", default="Legacy Local User")
    args = parser.parse_args()
    password = getpass.getpass("New legacy account password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation or len(password) < 10:
        raise SystemExit("Passwords must match and contain at least 10 characters.")
    user = claim_legacy_user(args.display_name, args.email, password)
    print(f"Legacy account credentials set for {user['email']}.")


if __name__ == "__main__":
    main()
