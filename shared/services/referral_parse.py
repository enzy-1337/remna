"""Парсинг deep-link /start ref_CODE."""


def parse_referral_code_from_start_args(args: str | None) -> str | None:
    if not args:
        return None
    token = args.strip().split()[0]
    if len(token) > 4 and token.lower().startswith("ref_"):
        return token[4:]
    return None
