"""Keep numeric llama-server timing/stop metadata, never raw child output."""
import json
import logging
import re


class EngineDiagnostics:
    def __init__(self, pid):
        self.pid, self.pending = pid, ''

    def feed(self, chunk):
        self.pending += chunk.decode('utf-8', 'replace')
        while '\n' in self.pending:
            line, self.pending = self.pending.split('\n', 1)
            metadata = None
            timing = re.search(r'\b(prompt eval|eval|total) time\s*=\s*([0-9.]+) ms /\s*(\d+) tokens', line)
            stopped = re.search(r'stop processing: n_tokens = (\d+), truncated = ([01])', line)
            if timing:
                try:metadata = {'kind': timing[1].replace(' ', '_'), 'milliseconds': float(timing[2]), 'tokens': int(timing[3])}
                except ValueError:pass
            elif stopped:
                metadata = {'kind': 'slot_stop', 'tokens': int(stopped[1]), 'truncated': bool(int(stopped[2]))}
            if metadata:
                logging.getLogger('uvicorn.error').info('qwen35_engine %s', json.dumps({'pid': self.pid, **metadata}))
        if len(self.pending) > 8192:
            self.pending = ''
