import argparse
import http.client
import json
import pathlib
import socket
import subprocess
import threading
import time
import urllib.request
from urllib.parse import urlsplit


parser = argparse.ArgumentParser(description='Check Threadfin playback through a backend Docker restart. Run with no other active Threadfin streams.')
parser.add_argument('--playlist', required=True, help='Path to the generated Threadfin M3U')
parser.add_argument('--channel', required=True)
parser.add_argument('--backend-url', default='http://127.0.0.1:5000')
parser.add_argument('--backend-container', default='magiogo-iptv-server')
parser.add_argument('--threadfin-host', default='127.0.0.1:34400')
parser.add_argument('--threadfin-container', default='threadfin')
args = parser.parse_args()

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def status():
    with opener.open(args.backend_url.rstrip('/') + '/status', timeout=2) as response:
        return json.load(response)


def media_pids():
    output = subprocess.check_output(['docker', 'top', args.threadfin_container, '-eo', 'pid,args'], text=True)
    return sorted(line.split(None, 1)[0] for line in output.splitlines()[1:] if 'ffmpeg' in line and '/channel/' in line)


class Player:
    def __init__(self, name, path):
        self.name, self.path = name, path
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.lock = threading.Lock()
        self.frames = [0, 0]
        self.errors = []
        self.failure = None
        self.bytes = 0
        self.arrivals = []
        self.connection = None
        self.sock = None
        self.decoder = None

    def count_frames(self):
        for line in self.decoder.stdout:
            if line.startswith((b'0,', b'1,')):
                with self.lock:
                    self.frames[int(line[:1])] += 1

    def collect_errors(self):
        for line in self.decoder.stderr:
            if line.strip():
                self.errors.append(line.decode(errors='replace').strip())

    def snapshot(self):
        with self.lock:
            return {'bytes': self.bytes, 'video_frames': self.frames[0], 'audio_frames': self.frames[1]}

    def run(self):
        try:
            self.decoder = subprocess.Popen([
                'docker', 'exec', '-i', args.threadfin_container, 'timeout', '80', 'ffmpeg', '-hide_banner', '-loglevel', 'error',
                '-threads', '2', '-i', 'pipe:0', '-map', '0:v:0', '-map', '0:a:0',
                '-threads', '1', '-fps_mode:v', 'passthrough', '-flush_packets', '1', '-f', 'framecrc', 'pipe:1',
            ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=65536)
            self.frame_reader = threading.Thread(target=self.count_frames, daemon=True)
            self.error_reader = threading.Thread(target=self.collect_errors, daemon=True)
            self.frame_reader.start()
            self.error_reader.start()
            self.connection = http.client.HTTPConnection(args.threadfin_host, timeout=25)
            started = time.monotonic()
            self.connection.request('GET', self.path, headers={'User-Agent': 'Magio-Gunicorn-Restart-Check'})
            response = self.connection.getresponse()
            self.sock = self.connection.sock or response.fp.raw._sock
            if response.status != 200:
                raise RuntimeError('Threadfin HTTP status ' + str(response.status))
            while not self.stop.is_set():
                chunk = response.read1(32768)
                if not chunk:
                    raise RuntimeError('Original Threadfin connection ended')
                now = time.monotonic()
                self.arrivals.append(now)
                with self.lock:
                    self.bytes += len(chunk)
                self.decoder.stdin.write(chunk)
                if self.bytes >= 2 * 1024 * 1024 and min(self.frames) >= 20:
                    self.ready.set()
                if not hasattr(self, 'first_byte_ms'):
                    self.first_byte_ms = round((now - started) * 1000)
        except Exception as error:
            if not self.stop.is_set():
                self.failure = type(error).__name__ + ': ' + str(error)
        finally:
            if self.connection:
                self.connection.close()
            if self.decoder:
                try:
                    self.decoder.stdin.close()
                except BrokenPipeError:
                    pass
                self.decoder.wait(timeout=12)
                self.frame_reader.join(2)
                self.error_reader.join(2)


lines = pathlib.Path(args.playlist).read_text().splitlines()
paths = {line.rsplit(',', 1)[-1]: urlsplit(lines[index + 1]).path for index, line in enumerate(lines[:-1]) if line.startswith('#EXTINF')}
players = [Player(args.channel, paths[args.channel])]
threads = [threading.Thread(target=player.run, daemon=True) for player in players]
try:
    for thread in threads:
        thread.start()
    for player in players:
        if not player.ready.wait(18):
            raise RuntimeError('Stream not ready: ' + str(player.failure))
    time.sleep(5)
    before = [player.snapshot() for player in players]
    pids_before = media_pids()
    assert len(pids_before) == 1, 'Expected one Threadfin FFmpeg source process'
    print('Before restart:', json.dumps(before), flush=True)
    old_started = json.loads(subprocess.check_output(['docker', 'inspect', args.backend_container]))[0]['State']['StartedAt']
    restart_started = time.monotonic()
    command = ['docker', 'restart', args.backend_container]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, timeout=20)
    for _ in range(50):
        try:
            state = json.loads(subprocess.check_output(['docker', 'inspect', args.backend_container]))[0]['State']
            if not state['Running'] or state['StartedAt'] == old_started:
                time.sleep(.1)
                continue
            with opener.open(args.backend_url.rstrip('/') + '/health', timeout=.5) as response:
                if json.load(response) == {'status': 'ok'}:
                    break
        except Exception:
            pass
        time.sleep(.1)
    else:
        raise RuntimeError('Backend did not restart')
    recovered = time.monotonic()
    at_recovery = [player.snapshot() for player in players]
    print('Backend HTTP recovered in ms:', round((recovered - restart_started) * 1000), flush=True)
    time.sleep(12)
    after = [player.snapshot() for player in players]
    current = status()
    for index, player in enumerate(players):
        assert player.failure is None, player.failure
        assert after[index]['bytes'] - at_recovery[index]['bytes'] >= 2 * 1024 * 1024
        assert after[index]['video_frames'] - at_recovery[index]['video_frames'] >= 50
        assert after[index]['audio_frames'] - at_recovery[index]['audio_frames'] >= 50
    assert current['cdn']['requests'] > 0
    assert current['api']['requests'] == 0
    pids_after = media_pids()
    assert pids_before == pids_after, 'Threadfin replaced its source FFmpeg process'
    for index, player in enumerate(players):
        gaps = [second - first for first, second in zip(player.arrivals, player.arrivals[1:]) if second >= restart_started and first <= recovered + 10]
        print(json.dumps({'channel': player.name, 'first_byte_ms': player.first_byte_ms,
                          'same_http_connection': player.failure is None,
                          'post_restart_video_frames': after[index]['video_frames'] - at_recovery[index]['video_frames'],
                          'post_restart_audio_frames': after[index]['audio_frames'] - at_recovery[index]['audio_frames'],
                          'max_data_gap_ms': round(max(gaps, default=0) * 1000),
                          'decode_errors': len(player.errors), 'ffmpeg_pid_retained': pids_before == pids_after}, ensure_ascii=False), flush=True)
    print('Restored sessions:', current['sessions']['restored'], 'new API requests:', current['api']['requests'], flush=True)
finally:
    for player in players:
        player.stop.set()
        if player.sock:
            try:
                player.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
    for thread in threads:
        thread.join(15)

for player in players:
    print('Decoder exit:', player.decoder.returncode, 'error lines:', len(player.errors), flush=True)
    assert player.decoder.returncode == 0 and not player.errors
