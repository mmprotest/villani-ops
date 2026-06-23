from pathlib import Path
import json

def write_events(path, events):
    Path(path).write_text("".join(json.dumps(e)+"\n" for e in events))
