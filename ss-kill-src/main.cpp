#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>

#include <cstring>
#include <iostream>
#include <string>

namespace {

void usage(const char *prog) {
    std::cerr << "Usage:\n"
              << "  " << prog << " tcp <local-ip> <local-port> <remote-ip> <remote-port>\n"
              << "  " << prog << " udp <local-ip> <local-port> <remote-ip> <remote-port>\n";
}

int kill_tcp_socket(const std::string &local_ip, unsigned short local_port,
                    const std::string &remote_ip, unsigned short remote_port) {
    (void)local_ip;
    (void)local_port;
    (void)remote_ip;
    (void)remote_port;
    // TODO: implement TCP socket killing.
    std::cerr << "kill_tcp_socket: not implemented\n";
    return 1;
}

int kill_udp_socket(const std::string &local_ip, unsigned short local_port,
                    const std::string &remote_ip, unsigned short remote_port) {
    (void)local_ip;
    (void)local_port;
    (void)remote_ip;
    (void)remote_port;
    // TODO: implement UDP socket killing.
    std::cerr << "kill_udp_socket: not implemented\n";
    return 1;
}

bool parse_port(const std::string &value, unsigned short &port) {
    try {
        std::size_t idx = 0;
        unsigned long parsed = std::stoul(value, &idx);
        if (idx != value.size() || parsed > 65535) {
            return false;
        }
        port = static_cast<unsigned short>(parsed);
        return true;
    } catch (...) {
        return false;
    }
}

} // namespace

int main(int argc, char *argv[]) {
    if (argc != 6) {
        usage(argv[0]);
        return 2;
    }

    std::string command = argv[1];
    std::string local_ip = argv[2];
    std::string remote_ip = argv[4];

    unsigned short local_port = 0;
    unsigned short remote_port = 0;
    if (!parse_port(argv[3], local_port) || !parse_port(argv[5], remote_port)) {
        std::cerr << "Invalid port\n";
        usage(argv[0]);
        return 2;
    }

    if (command == "tcp") {
        return kill_tcp_socket(local_ip, local_port, remote_ip, remote_port);
    }
    if (command == "udp") {
        return kill_udp_socket(local_ip, local_port, remote_ip, remote_port);
    }

    std::cerr << "Unknown command: " << command << "\n";
    usage(argv[0]);
    return 2;
}
