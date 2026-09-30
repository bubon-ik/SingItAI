#!/usr/bin/env python3
"""Opt-in live check of how Jev reads the web chat's messages: intent and country, nothing else.

Asks TypeSafe's Jev the same question the web agent asks (sign402_gateway.web_agent.Jev) for each
message below and compares its reading with what the message wants. It buys nothing, sends nothing
and touches no account; each message is one Jev call, a fraction of a cent.

    TYPESAFE_API_KEY=... python scripts/check-web-jev.py --live

Messages are the ones people wrote on the web page (30 September 2026), phrasings around them in
English, Russian, Czech and German, and conversation that must stay conversation.
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sign402-gateway"))

FOOD, PLACES = {"food"}, {"live_data"}
EAT = FOOD | PLACES  # either answers "I want to eat": food cards or places to eat
CASES = [
    # (message, intents that are right, country or None)
    ("I'm hungry in Prague", FOOD, "CZ"),
    ("I'm hungry, and I'm now in Czech Republic", FOOD, "CZ"),
    ("I wanna food in Prague", FOOD, "CZ"),
    ("food in Prague", EAT, "CZ"),
    ("pizza in Prague", EAT, "CZ"),
    ("order lunch in Prague", FOOD, "CZ"),
    ("I'm in Prague and haven't eaten since morning", EAT, "CZ"),
    ("something to eat near me in Brno", EAT, "CZ"),
    ("sushi delivery in Berlin", FOOD, "DE"),
    ("I'm starving, I'm in Warsaw", FOOD, "PL"),
    ("I want to go to a supermarket in Prague", FOOD, "CZ"),
    ("need groceries in Berlin", FOOD, "DE"),
    ("я голоден, я в Праге", FOOD, "CZ"),
    ("хочу есть в Праге", FOOD, "CZ"),
    ("где тут пожрать в Праге", EAT, "CZ"),
    ("хочу купить продукты в Праге", FOOD, "CZ"),
    ("Mám hlad v Praze", FOOD, "CZ"),
    ("chci jídlo v Praze", FOOD, "CZ"),
    ("Ich habe Hunger in Berlin", FOOD, "DE"),
    ("Ich will Essen in Berlin", FOOD, "DE"),
    ("Restaurants in Prague", PLACES, "CZ"),
    ("Restaurants in Czechia", PLACES, "CZ"),
    ("restaurace v Praze", PLACES, "CZ"),
    ("Wo kann man in Berlin gut essen", EAT, "DE"),
    ("где в Праге поесть", EAT, "CZ"),
    ("hotels in Prague", PLACES, "CZ"),
    ("best cafes near Charles Bridge", PLACES, None),
    ("things to do in Rome", PLACES, "IT"),
    ("weather in Prague", PLACES, None),
    ("погода в Праге", PLACES, None),
    ("will it rain in Berlin tomorrow", PLACES, None),
    ("I need internet in Germany", {"esim"}, "DE"),
    ("any good eSIM for Germany", {"esim"}, "DE"),
    ("интернет в Германии", {"esim"}, "DE"),
    ("data for my trip to Spain", {"esim"}, "ES"),
    ("Steam gift card in Germany", {"gift_card"}, "DE"),
    ("steam card germany", {"gift_card"}, "DE"),
    ("Amazon voucher 50 EUR Germany", {"gift_card"}, "DE"),
    ("dárkový poukaz Alza", {"gift_card"}, None),
    ("What can my agent spend today?", {"status"}, None),
    ("what is my balance", {"status"}, None),
    ("сколько я могу потратить", {"status"}, None),
    ("Kolik můžu utratit?", {"status"}, None),
    ("show my purchases", {"purchases"}, None),
    ("what did I buy", {"purchases"}, None),
    ("show me the code of my last gift card", {"purchases"}, None),
    ("Set a $30 daily limit, $10 per purchase", {"set_limits"}, None),
    ("raise my per purchase limit to 25", {"set_limits"}, None),
    ("call the restaurant at +420123456789 and book a table for 2 at 8pm", {"call"}, None),
    ("tell me a joke", {"chat"}, None),
    ("The Hunger Games", {"chat"}, None),
    ("I work in a restaurant in Prague", {"chat"}, None),
    ("a recipe for pizza", {"chat"}, None),
    ("the history of Czech food", {"chat"}, None),
    ("how are you?", {"chat"}, None),
    ("what is x402?", {"chat"}, None),
    ("explain how my limiter works", {"chat"}, None),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--live", action="store_true", help="call TypeSafe's Jev for real")
    args = parser.parse_args()
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not args.live or not key:
        print("Run with --live and TYPESAFE_API_KEY set. Nothing was called.")
        return 2
    from sign402_gateway import web_agent as wg

    jev = wg.Jev(key, os.environ.get("SIGN402_TYPESAFE_MODEL", "") or "jev-latest")
    raw: dict = {}
    ask = jev._ask

    def recording(text, questions):  # the choice and confidence behind each reading, for the report
        raw.clear()
        raw.update(ask(text, questions))
        return raw
    jev._ask = recording

    right = country_right = 0
    for text, wanted, country in CASES:
        try:
            read = jev(text)
        except wg.AgentUnavailable:
            read = {"intent": "(no answer)", "country": ""}
        top = raw.get("intent") or {}
        ok = read["intent"] in wanted
        right += ok
        country_ok = country is None or read.get("country") == country
        country_right += country_ok
        print(f"{'ok ' if ok else 'BAD'} {'  ' if country_ok else 'C!'} {text!r:62} -> {read['intent']:<12} "
              f"(Jev: {top.get('choice')} {float(top.get('confidence') or 0):.2f}; country {read.get('country') or '-'})"
              f"{'' if ok else '  want ' + '/'.join(sorted(wanted))}")
    print(f"\nintent right: {right}/{len(CASES)}; country right: {country_right}/{len(CASES)}")
    return 0 if right == len(CASES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
