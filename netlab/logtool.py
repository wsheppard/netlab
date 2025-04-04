import argparse
import sys
import os
import hashlib
import json
import zstandard as zstd

from typing import Any
from netlab.eventbus import Event
import hashlib

# Whoosh imports
from whoosh.index import create_in, open_dir
from whoosh.fields import Schema, TEXT, ID, NUMERIC
from whoosh.qparser import QueryParser
from whoosh.query import Term, Not, And, Wildcard, NumericRange

from rich.console import Console

console = Console()
colors = ["red", "green", "blue", "cyan", "magenta", "yellow", "white", "bright_black"]


def guid_to_color(guid):
    h = hashlib.md5(guid.encode()).hexdigest()
    return colors[int(h, 16) % len(colors)]

def read_zst_lines(path):
    with open(path, 'rb') as fh:
        dctx = zstd.ZstdDecompressor()
        with dctx.stream_reader(fh) as reader:
            buffer = b""
            while True:
                chunk = reader.read(16384)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    yield line.decode('utf-8')
            if buffer:
                yield buffer.decode('utf-8')

schema = Schema(
    guid=ID(stored=True),
    name=TEXT(stored=True),
    evtype=ID(stored=True),
    data=TEXT(stored=True),
    timestamp=NUMERIC(numtype=float, stored=True)
)

from collections import Counter, defaultdict


def evtypes(zst_path: str) -> None:
    ix = open_existing_index(zst_path)
    with ix.searcher() as searcher:
        counter = Counter()
        for hit in searcher.documents():
            evtype = hit.get("evtype", "")
            counter[evtype] += 1

        print(f"{'EvType':<30}  {'Count':>5}")
        print("-" * 40)
        for evtype, count in counter.most_common():
            print(f"{evtype:<30}  {count:>5}")


def guids(zst_path: str) -> None:
    ix = open_existing_index(zst_path)
    with ix.searcher() as searcher:
        counter = Counter()
        names = defaultdict(str)
        for hit in searcher.documents():
            guid = hit["guid"]
            name = hit.get("name", "")
            if str(name) == str(guid):
                name = ""
            counter[guid] += 1
            if not names[guid]:
                names[guid] = name

        print(f"{'GUID':<36}  {'Count':>5}  Name")
        print("-" * 60)
        for guid, count in counter.most_common():
            print(f"{guid:<36}  {count:>5}  {names[guid]}")

def get_index_path(zst_path: str) -> str:
    hashed = hashlib.md5(zst_path.encode('utf-8')).hexdigest()
    return f'/tmp/{hashed}'

def build_index(zst_path: str) -> None:
    index_path = get_index_path(zst_path)
    print(f"Building index {index_path}")
    new_index = False
    if not os.path.exists(index_path) or not os.listdir(index_path):
        os.makedirs(index_path, exist_ok=True)
        ix = create_in(index_path, schema)
        new_index = True
    else:
        ix = open_dir(index_path)

    writer = ix.writer()
    lines = read_zst_lines(zst_path)
    count = 0
    for line in lines:
        try:
            obj = json.loads(line)
            event = Event[Any].model_construct(**obj)
            data_dict = getattr(event, 'data', None)
            data_json = json.dumps(data_dict, ensure_ascii=False) if data_dict else ""
            ts = float(getattr(event, 'timestamp', 0.0) or 0.0)
            writer.add_document(
                guid=str(event.guid),
                name=event.name or "",
                evtype=event.evtype or "",
                data=data_json,
                timestamp=ts
            )
            count += 1
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            print(f"Warning: failed to parse line: {e}", file=sys.stderr)

    writer.commit()
    if new_index:
        print(f"Created new index at {index_path}")
    else:
        print(f"Using existing index at {index_path}")
    print(f"Indexed {count} log entries")

def open_existing_index(zst_path: str):
    index_path = get_index_path(zst_path)
    if not os.path.exists(index_path) or not os.listdir(index_path):
        build_index(zst_path)
    print(f"Using index at {index_path}")
    return open_dir(index_path)

def build_field_filters(filters, field):
    if not filters:
        return []
    queries = []
    for f in filters:
        neg = f.startswith("!")
        pat = f[1:] if neg else f
        q = Wildcard(field, pat) if "*" in pat else Term(field, pat)
        queries.append(Not(q) if neg else q)
    return queries

def print_hits(hits):
    for hit in sorted(hits, key=lambda h: float(h["timestamp"])):
        color = guid_to_color(hit["guid"])
        line = (
            f"[{int(hit['timestamp'])}] "
            f"{hit['name']}|{hit['evtype']} "
            f"{hit['data']}"
        )
        console.print(line, style=color, highlight=False)




def search_by_term(zst_path: str, query_str: str, guid_filters, name_filters, evtype_filters) -> None:
    ix = open_existing_index(zst_path)
    with ix.searcher() as searcher:
        query_parts = []

        if query_str:
            parser = QueryParser("data", schema=ix.schema)
            query_parts.append(parser.parse(query_str))

        query_parts += (
            build_field_filters(guid_filters, "guid") +
            build_field_filters(name_filters, "name") +
            build_field_filters(evtype_filters, "evtype")
        )

        full_query = And(query_parts) if query_parts else None
        results = searcher.search(full_query, limit=None) if full_query else searcher.documents()
        print(f"Found {len(results)} results")
        print_hits(results)

def search_around_timestamp(zst_path: str, around_ts: float, window: float, guid_filters, name_filters, evtype_filters) -> None:
    ix = open_existing_index(zst_path)
    lower = around_ts - window
    upper = around_ts + window
    with ix.searcher() as searcher:
        base_query = NumericRange("timestamp", lower, upper, startexcl=False, endexcl=False)
        filters = (
            build_field_filters(guid_filters, "guid") +
            build_field_filters(name_filters, "name") +
            build_field_filters(evtype_filters, "evtype")
        )
        full_query = And([base_query] + filters) if filters else base_query
        results = searcher.search(full_query, limit=None)
        print(f"Found {len(results)} results for timestamp range [{lower}, {upper}]")
        print_hits(results)

def main():
    parser = argparse.ArgumentParser(description="LogTool with Whoosh integration.")
    parser.add_argument('--build', action='store_true', help="Build/update the index for the specified log file.")
    parser.add_argument('--term', help="Search logs by substring in the 'data' field.")
    parser.add_argument('--around-ts', type=float, help="Show logs around this timestamp (+-5s by default).")
    parser.add_argument('--window', type=float, default=5.0, help="Window in seconds for --around-ts.")
    parser.add_argument('--guid', action='append', help="Filter by guid (supports *, ! for negation)")
    parser.add_argument('--name', action='append', help="Filter by name (supports *, ! for negation)")
    parser.add_argument('--evtype', action='append', help="Filter by evtype (supports *, ! for negation)")
    parser.add_argument('zst_file', help="Path to .zst JSONL log file")
    parser.add_argument('--guids', action='store_true', help="Show unique GUIDs with count and name.")
    parser.add_argument('--evtypes', action='store_true', help="Show unique evtypes with count.")


    args = parser.parse_args()

    if args.guids:
        guids(args.zst_file)
    elif args.evtypes:
        evtypes(args.zst_file)
    elif args.term or args.guid or args.name or args.evtype:
        search_by_term(
            args.zst_file,
            args.term,  # this can be None
            args.guid,
            args.name,
            args.evtype
        )
    elif args.around_ts is not None:
        search_around_timestamp(
            args.zst_file,
            args.around_ts,
            args.window,
            args.guid,
            args.name,
            args.evtype
        )
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

