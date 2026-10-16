"""Bounded text/Schema calls to the same owned Qwen3.5 vision process."""
import asyncio
import json
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

import httpx

from backend.app.models.qwen import QwenAdapter
from backend.app.models.base import ModelError, ModelNotConfigured, ModelTimeout
from backend.app.models.response import split_response
from backend.app.resources.manager import GPUUnsafe
from backend.app.resources.memory import check_stream_memory


_request = ContextVar('qwen35_request_id', default=None)
_job = ContextVar('qwen35_job_id', default=None)


@contextmanager
def trace_request(request_id):
    token = _request.set(str(request_id))
    try:
        yield
    finally:
        _request.reset(token)


@contextmanager
def trace_job(job_id):
    token = _job.set(str(job_id))
    try:
        yield
    finally:
        _job.reset(token)


def log_event(event, **metadata):
    # Only caller-selected numeric/enum metadata. Never bodies, paths or reasoning.
    logging.getLogger('uvicorn.error').info('qwen35_diagnostic %s', json.dumps({
        'event': event, 'request_id': _request.get(), 'job_id': _job.get(), **metadata}))


class Qwen35Adapter(QwenAdapter):
    @staticmethod
    def _oom():
        try:
            return int(dict(line.split() for line in Path('/proc/vmstat').read_text().splitlines())['oom_kill'])
        except (OSError, ValueError, KeyError):
            raise GPUUnsafe('memory_safety_telemetry_unavailable') from None

    async def _generate(self, messages, schema=None):
        if not self.settings.endpoint:
            raise ModelNotConfigured('Qwen3.5 is not configured')
        payload = {'model': self.settings.model_name, 'stream': False, 'max_tokens': 3072,
                   'temperature': 0.2, 'chat_template_kwargs': {'enable_thinking': False},
                   'messages': [{'role': m.role, 'content': m.content} for m in messages]}
        if schema is not None:
            payload['response_format'] = {'type': 'json_schema', 'json_schema': {
                'name': 'pitch_chat_contract', 'strict': True, 'schema': schema}}
            payload['temperature'] = 0
        started = time.monotonic()
        log_event('request', roles=[m.role for m in messages], message_chars=[len(m.content) for m in messages],
                  schema=schema is not None, max_tokens=3072, thinking=False)
        check_stream_memory(2048)
        baseline = self._oom()
        task = asyncio.create_task(asyncio.wait_for(
            self.client.post(self.settings.endpoint, json=payload, timeout=self.settings.timeout_seconds),
            timeout=self.settings.timeout_seconds))
        failure = None
        response = None
        try:
            while not task.done():
                if failure is None:
                    try:
                        check_stream_memory(2048)
                        if self._oom() != baseline:
                            raise GPUUnsafe('oom_observed')
                    except GPUUnsafe as error:
                        failure = error  # Keep lease until terminal; never kill inference.
                await asyncio.wait({task}, timeout=.5)
            response = await asyncio.shield(task)
            if failure:
                raise failure
            response.raise_for_status()
            raw = response.json()
            choice = raw['choices'][0]
            message = choice['message']
            usage = raw.get('usage') if isinstance(raw.get('usage'), dict) else {}
            log_event('response', http_status=response.status_code,
                      finish_reason=choice.get('finish_reason') if choice.get('finish_reason') in {'stop', 'length'} else 'other',
                      elapsed_seconds=round(time.monotonic()-started, 3),
                      prompt_tokens=usage.get('prompt_tokens') if isinstance(usage.get('prompt_tokens'), int) else None,
                      completion_tokens=usage.get('completion_tokens') if isinstance(usage.get('completion_tokens'), int) else None)
            if (raw.get('model') != self.settings.model_name or choice.get('finish_reason') != 'stop'
                    or message.get('tool_calls') or message.get('function_call')):
                raise ValueError('invalid_qwen35_response')
            return split_response(message)
        except asyncio.CancelledError:
            # The durable job can cancel immediately, but this operation retains its
            # resource lease until the bounded HTTP call actually ends. Closing the
            # socket is not proof that llama-server stopped generating.
            log_event('cancel_drain_started')
            try:
                terminal = await asyncio.shield(task)
                log_event('cancel_drain_complete', http_status=terminal.status_code,
                          elapsed_seconds=round(time.monotonic()-started, 3))
            except (httpx.HTTPError, TimeoutError) as error:
                log_event('cancel_drain_uncertain', exception_type=type(error).__name__)
            raise
        except (httpx.TimeoutException, TimeoutError):
            log_event('failure', cause='timeout', exception_type='ModelTimeout', elapsed_seconds=round(time.monotonic()-started, 3))
            raise ModelTimeout('Qwen3.5の応答がタイムアウトしました。') from None
        except ModelError as error:
            log_event('failure', cause=error.classification or 'invalid_final', exception_type=type(error).__name__,
                      http_status=response.status_code if response is not None else None)
            raise
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as error:
            log_event('failure', cause='http_error' if response is not None and response.status_code>=400 else 'invalid_response',
                      exception_type=type(error).__name__, http_status=response.status_code if response is not None else None)
            raise ModelError('Qwen3.5の接続または応答形式に問題があります。',
                             http_status=response.status_code if response is not None else None) from None
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                # Managed stop still requires proof of an idle slot.
