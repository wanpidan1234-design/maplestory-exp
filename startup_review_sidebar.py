"""Prepare one persistent desktop-visible thread without calling a model.

The runner invokes this helper inside its bounded Windows Job, then uses
``codex exec resume`` for the actual review. No daemon or polling is installed.
"""
from __future__ import annotations

import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import uuid


PREPARE_SECONDS = 30


def _identity(value):
    result = str(uuid.UUID(value))
    if result != value:
        raise ValueError('canonical thread/event UUID required')
    return result


def prepare_with_rpc(call, event_id, cwd):
    """Only metadata/history RPCs; never turn/start, queue, or inference."""
    _identity(event_id)
    call('initialize', {
        'clientInfo': {'name': 'maple_exp_review', 'title': 'Maple EXP Review', 'version': '1.0'},
        'capabilities': {'experimentalApi': True},
    })
    thread = call('thread/start', {
        'cwd': cwd, 'approvalPolicy': 'never', 'permissions': 'maple_review',
    })['thread']
    thread_id = _identity(thread['id'])
    if thread.get('source') not in {'cli', 'vscode'}:
        raise ValueError('app-server thread source is not desktop-listable')
    title = '冒险岛后台评审 · ' + event_id[:8]
    call('thread/name/set', {'threadId': thread_id, 'name': title})
    # thread/start alone is lazy: without a history item the thread is lost at
    # server exit. This context item persists it without starting a model turn.
    call('thread/inject_items', {'threadId': thread_id, 'items': [{
        'type': 'message', 'role': 'user', 'content': [{
            'type': 'input_text',
            'text': ('冒险岛后台评审会话已准备。事件：' + event_id
                     + '。此条仅用于保存会话；尚未开始评审，实际任务及授权以随后宿主发送的启动提示为准。'),
        }],
    }]})
    saved = call('thread/read', {'threadId': thread_id, 'includeTurns': False})['thread']
    if saved['id'] != thread_id or saved.get('source') != thread['source']:
        raise ValueError('saved sidebar thread identity/source mismatch')
    return {'status': 'prepared', 'thread_id': thread_id, 'source': thread['source'], 'title': title}


def _rpc_process(command, event_id, cwd):
    """The parent Job bounds this process and every app-server descendant."""
    deadline = time.monotonic() + 20
    with subprocess.Popen(command + ['app-server', '--stdio'], cwd=cwd,
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr,
                          creationflags=subprocess.CREATE_NO_WINDOW) as process:
        responses = queue.Queue()

        def read():
            try:
                for line in process.stdout:
                    responses.put(json.loads(line))
            except Exception as error:
                responses.put(error)
            finally:
                responses.put(EOFError('app-server closed'))

        threading.Thread(target=read, name='sidebar-rpc-reader', daemon=True).start()
        sequence = 0

        def call(method, params):
            nonlocal sequence
            sequence += 1
            process.stdin.write((json.dumps({'id': sequence, 'method': method, 'params': params}) + '\n').encode('utf-8'))
            process.stdin.flush()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('sidebar RPC deadline reached')
                response = responses.get(timeout=remaining)
                if isinstance(response, Exception):
                    raise response
                if response.get('id') == sequence:
                    if 'error' in response:
                        raise RuntimeError('sidebar RPC rejected ' + method)
                    return response['result']

        try:
            return prepare_with_rpc(call, event_id, cwd)
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


def prepare_sidebar_thread(directory, event_id, cli_command, permissions, cwd, environment, guard):
    """Best-effort preparation, with a durable receipt and no automatic retry."""
    from startup_review_process import run_bounded
    directory = Path(directory)
    request = {'event_id': _identity(event_id), 'command': list(cli_command) + list(permissions), 'cwd': str(cwd)}
    try:
        with (directory / 'sidebar-stdout.json').open('wb') as output, (directory / 'sidebar-stderr.log').open('wb') as errors:
            code = run_bounded([sys.executable, str(Path(__file__).resolve())],
                               input_bytes=json.dumps(request).encode('utf-8'), stdout=output, stderr=errors,
                               cwd=str(cwd), env=environment, timeout=PREPARE_SECONDS, guard=guard)
        if code != 0:
            raise RuntimeError('sidebar helper failed')
        result = json.loads((directory / 'sidebar-stdout.json').read_text(encoding='utf-8'))
        _identity(result['thread_id'])
        if result.get('status') != 'prepared' or result.get('source') not in {'cli', 'vscode'}:
            raise ValueError('invalid sidebar preparation receipt')
    except Exception as error:
        result = {'status': 'failed', 'error_type': type(error).__name__}
    receipt = directory / 'sidebar.json'
    temporary = receipt.with_suffix('.json.tmp')
    try:
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(receipt)
    except OSError as error:
        result['receipt_error'] = type(error).__name__
    return result


if __name__ == '__main__':
    request = json.load(sys.stdin)
    result = _rpc_process(request['command'], request['event_id'], request['cwd'])
    # ASCII keeps the Windows pipe independent of the host console code page.
    print(json.dumps(result, ensure_ascii=True))
