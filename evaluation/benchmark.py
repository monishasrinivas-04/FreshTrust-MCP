import json, time
from pathlib import Path
def save_measurement(record,path="data/evaluation_results.jsonl"):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    with open(path,"a",encoding="utf-8") as f: f.write(json.dumps(record,default=str)+"\n")
