import socket, time

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.connect(("127.0.0.1", 8081))

while True:
    start = time.time()
    s.sendall(b"PING")
    s.recv(4)
    end = time.time()
    print(f"RTT: {(end - start) * 1000:.2f} ms")
    time.sleep(1)
