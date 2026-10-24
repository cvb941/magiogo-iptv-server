import sys

bind = '0.0.0.0:5000'
workers = 1
worker_class = 'gunicorn_worker.StreamingThreadWorker'
threads = 16
preload_app = False
timeout = 60
graceful_timeout = 8
# Short-lived DASH requests should drain promptly during a container restart.
keepalive = 0
control_socket_disable = True
worker_tmp_dir = '/dev/shm'
accesslog = None
errorlog = '-'


def on_starting(server):
    if server.cfg.workers != 1 or server.cfg.preload_app:
        raise RuntimeError('This application requires one worker without preloading')


def worker_exit(server, worker):
    module = sys.modules.get('app')
    if module is not None:
        module.shutdown()
