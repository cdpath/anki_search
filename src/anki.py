import sys
import time
import os
import json
import urllib.request
import argparse
import uuid
import logging
import html.parser
import pickle
import tempfile
import shutil

env = os.environ.get

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger()


ANKI_CONNECT_URL = env("ANKI_CONNECT_URL", "http://localhost:8765")
FRONT_FIELDS = [i.strip() for i in env("FRONT_FIELDS", "Front,entry").split(",")]
BACK_FIELDS = [i.strip() for i in env("BACK_FIELDS", "Back,Tags,definition").split(",")]

CACHE_FILE = "anki_tags_cache.pkl"
CACHE_EXPIRE_SECONDS = 24 * 60 * 60  # 24 hours


logger.debug(
    "AnkiConnectURL: %s, Front: %s, Back: %s",
    ANKI_CONNECT_URL,
    FRONT_FIELDS,
    BACK_FIELDS,
)


class HTMLStripper(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.reset()
        self.strict = False
        self.convert_charrefs = True
        self.fed = []
        self.ignore = False

    def handle_starttag(self, tag, attrs):
        if tag == "style":
            self.ignore = True

    def handle_endtag(self, tag):
        if tag == "style":
            self.ignore = False

    def handle_data(self, d):
        if not self.ignore:
            self.fed.append(d)

    def get_data(self):
        return "".join(self.fed)


def strip_html(html):
    s = HTMLStripper()
    s.feed(html)
    stripped = s.get_data().strip()
    if "\n" in stripped:
        stripped = stripped.split("\n")[0] + " [...]"
    return stripped


def request(action, **params):
    return {"action": action, "params": params, "version": 6}


def invoke(action, **params):
    requestJson = json.dumps(request(action, **params)).encode("utf-8")
    response = json.load(
        urllib.request.urlopen(urllib.request.Request(ANKI_CONNECT_URL, requestJson))
    )
    if len(response) != 2:
        raise Exception("response has an unexpected number of fields")
    if "error" not in response:
        raise Exception("response is missing required error field")
    if "result" not in response:
        raise Exception("response is missing required result field")
    if response["error"] is not None:
        raise Exception(response["error"])
    return response["result"]


def find_notes(query):
    notes = invoke("findNotes", query=query)
    note_info = invoke("notesInfo", notes=notes)
    return note_info


def notes_info(notes):
    return invoke("notesInfo", notes=notes)


def find_cards(query):
    cards = invoke("findCards", query=query)
    card_info = invoke("cardsInfo", cards=cards)
    return card_info


def cards_info(cards):
    return invoke("cardsInfo", cards=cards)


def gui_browse(query):
    card_ids = invoke("guiBrowse", query=query)
    # card_info = invoke("cardsInfo", cards=card_ids)
    # return format_card_info(card_info)


def get_tags(query=None):
    tags = cache_tags()
    if query:
        tags = [tag for tag in tags if query in tag]
    logger.info("filtered Tags are: %s", tags)
    return tags


def cache_tags(force_refresh=False):
    if not force_refresh and os.path.exists(CACHE_FILE):
        # Check the file's modify time to decide whether to use cache
        if time.time() - os.path.getmtime(CACHE_FILE) < CACHE_EXPIRE_SECONDS:
            try:
                with open(CACHE_FILE, "rb") as f:
                    logger.debug("Read tags from cache")
                    return pickle.load(f)
            except EOFError:
                logger.error("Cache file is corrupted, refreshing cache")

    # No valid cache, get from AnkiConnect
    tags = invoke("getTags")
    with tempfile.NamedTemporaryFile("wb", delete=False) as tempf:
        pickle.dump(tags, tempf)
    shutil.move(tempf.name, CACHE_FILE)
    return tags


def gen_question(note, fields=FRONT_FIELDS):
    for field in fields:
        if field in note.get("fields", {}):
            res = note["fields"][field]["value"]
            return strip_html(res)


def gen_answer(note, fields=BACK_FIELDS):
    for field in fields:
        if field in note.get("fields", {}):
            res = note["fields"][field]["value"]
            return strip_html(res)


def alfred_tag(tags):
    items = []
    for tag in tags:
        item = {
            "uid": str(uuid.uuid4()),
            "title": tag,
            "arg": f"tag:{tag}",
        }
        items.append(item)
    return {"items": items}


def alfred_note(note_info):
    items = []
    for note in note_info:
        logger.info("Note content: %s", note)
        title, subtitle = gen_question(note), gen_answer(note)
        if not title:
            continue
        item = {
            "uid": str(uuid.uuid4()),
            "title": title,
            "subtitle": subtitle,
            "arg": f"nid:{note['noteId']}",
        }
        items.append(item)
    return {"items": items}


def alfred_msg(title, subtitle, icon="./error.png"):
    return {
        "items": [
            {
                "uid": str(uuid.uuid4()),
                "title": title,
                "subtitle": subtitle,
                "icon": {"path": icon},
            }
        ]
    }


def main():
    parser = argparse.ArgumentParser(description="Interact with Anki via AnkiConnect.")
    subparsers = parser.add_subparsers(dest="action")

    query_anki_parser = subparsers.add_parser("guiBrowse")
    query_anki_parser.add_argument("query")

    find_notes_parser = subparsers.add_parser("findNotes")
    find_notes_parser.add_argument("query")

    cards_info_parser = subparsers.add_parser("cardsInfo")
    cards_info_parser.add_argument("cards", nargs="+", type=int)

    get_tags_parser = subparsers.add_parser("getTags")
    get_tags_parser.add_argument("--query", default=None)

    refresh_cache_parser = subparsers.add_parser("refreshAnkiCache")

    args = parser.parse_args()
    logger.debug(args)

    payload = None
    try:
        if args.action == "findNotes":
            note_info = find_notes(args.query)
            if not note_info:
                raise ValueError()
            payload = alfred_note(note_info)

        elif args.action == "getTags":
            tags = get_tags(args.query)
            if not tags:
                raise ValueError()
            payload = alfred_tag(tags)

        elif args.action == "refreshAnkiCache":
            cache_tags(force_refresh=True)

        elif args.action == "cardsInfo":
            card_info = cards_info(args.cards)
            if not note_info:
                raise ValueError()
            payload = alfred_note(card_info)

        elif args.action == "guiBrowse":
            card_info = gui_browse(args.query)
            # print(json.dumps(alfred_output(card_info), indent=2))
    except ValueError:
        error = alfred_msg("Not Found", "Please try another query")
        print(json.dumps(error))
    except urllib.error.URLError:
        error = alfred_msg(
            "Is Anki Running?", "Remember to install AnkiConnect as well"
        )
        print(json.dumps(error))
    else:
        if payload is not None:
            print(json.dumps(payload))


if __name__ == "__main__":
    main()
