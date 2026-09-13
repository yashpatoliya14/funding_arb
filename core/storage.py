import os
import json
import logging
import tempfile
import shutil
from typing import Dict, Any, Optional

log = logging.getLogger("storage")

class AtomicStorage:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.state_file = os.path.join(data_dir, "state.json")
        self.state = self._load()

    def _load(self) -> Dict[str, Any]:
        if not os.path.exists(self.state_file):
            return {"opportunities": [], "trades": [], "positions": {}}
            
        try:
            with open(self.state_file, "r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            log.error("Corrupted storage file detected, attempting recovery")
            # If there's a .bak file, try it
            bak_file = self.state_file + ".bak"
            if os.path.exists(bak_file):
                try:
                    with open(bak_file, "r") as f:
                        return json.load(f)
                except json.JSONDecodeError:
                    pass
            # Unrecoverable
            return {"opportunities": [], "trades": [], "positions": {}}

    def _save(self):
        fd, tmp_path = tempfile.mkstemp(dir=self.data_dir, prefix="state_", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.state, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
        except Exception as e:
            os.remove(tmp_path)
            raise e
            
        # Atomic rename
        if os.path.exists(self.state_file):
            bak_file = self.state_file + ".bak"
            # Windows workaround for atomic rename replacing existing files
            if os.path.exists(bak_file):
                os.remove(bak_file)
            shutil.move(self.state_file, bak_file)
            
        shutil.move(tmp_path, self.state_file)

    def persist_opportunity(self, opp: dict):
        self.state.setdefault("opportunities", []).append(opp)
        self._save()
        
    def persist_trade(self, trade: dict):
        self.state.setdefault("trades", []).append(trade)
        self._save()
        
    def save_position(self, symbol: str, position_data: dict):
        self.state.setdefault("positions", {})[symbol] = position_data
        self._save()
        
    def clear_position(self, symbol: str):
        if symbol in self.state.get("positions", {}):
            del self.state["positions"][symbol]
            self._save()

    def get_state(self) -> Dict[str, Any]:
        return self.state
