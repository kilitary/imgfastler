#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/ip.h>
#include <netinet/ip6.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/types.h>
#include <unistd.h>

#include <linux/inet_diag.h>
#include <linux/netlink.h>
#include <linux/sock_diag.h>

#include <cerrno>
#include <cstring>
#include <iostream>
#include <string>
#include <vector>

namespace {

void usage(const char *prog) {
    std::cerr << "Usage:\n"
              << "  " << prog << " tcp <local-ip> <local-port> <remote-ip> <remote-port>\n"
              << "  " << prog << " udp <local-ip> <local-port> <remote-ip> <remote-port>\n";
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

int detect_family(const std::string &local_ip, const std::string &remote_ip, uint8_t &family) {
    in_addr v4{};
    in6_addr v6{};
    bool local_v4 = inet_pton(AF_INET, local_ip.c_str(), &v4) == 1;
    bool local_v6 = inet_pton(AF_INET6, local_ip.c_str(), &v6) == 1;
    bool remote_v4 = inet_pton(AF_INET, remote_ip.c_str(), &v4) == 1;
    bool remote_v6 = inet_pton(AF_INET6, remote_ip.c_str(), &v6) == 1;

    if (local_v4 && remote_v4) {
        family = AF_INET;
        return 0;
    }
    if (local_v6 && remote_v6) {
        family = AF_INET6;
        return 0;
    }
    std::cerr << "Local and remote IPs must be valid and of the same family (both IPv4 or both IPv6)\n";
    return -1;
}

int kill_inet_socket(uint8_t family, uint8_t protocol,
                     const std::string &local_ip, unsigned short local_port,
                     const std::string &remote_ip, unsigned short remote_port) {
    int fd = socket(AF_NETLINK, SOCK_RAW | SOCK_CLOEXEC, NETLINK_SOCK_DIAG);
    if (fd < 0) {
        std::cerr << "Failed to open NETLINK_SOCK_DIAG socket: " << std::strerror(errno) << "\n";
        return 1;
    }

    struct {
        nlmsghdr nlh;
        inet_diag_req_v2 req;
    } msg{};

    msg.nlh.nlmsg_len = sizeof(msg);
    msg.nlh.nlmsg_type = SOCK_DESTROY;
    msg.nlh.nlmsg_flags = NLM_F_REQUEST | NLM_F_ACK;
    msg.nlh.nlmsg_seq = 1;
    msg.nlh.nlmsg_pid = 0;

    msg.req.sdiag_family = family;
    msg.req.sdiag_protocol = protocol;
    msg.req.idiag_states = 0;

    msg.req.id.idiag_sport = htons(local_port);
    msg.req.id.idiag_dport = htons(remote_port);
    msg.req.id.idiag_if = 0;
    msg.req.id.idiag_cookie[0] = INET_DIAG_NOCOOKIE;
    msg.req.id.idiag_cookie[1] = INET_DIAG_NOCOOKIE;

    if (family == AF_INET) {
        in_addr src{};
        in_addr dst{};
        if (inet_pton(AF_INET, local_ip.c_str(), &src) != 1 ||
            inet_pton(AF_INET, remote_ip.c_str(), &dst) != 1) {
            std::cerr << "Invalid IPv4 address\n";
            close(fd);
            return 2;
        }
        std::memcpy(msg.req.id.idiag_src, &src, sizeof(src));
        std::memcpy(msg.req.id.idiag_dst, &dst, sizeof(dst));
    } else if (family == AF_INET6) {
        in6_addr src{};
        in6_addr dst{};
        if (inet_pton(AF_INET6, local_ip.c_str(), &src) != 1 ||
            inet_pton(AF_INET6, remote_ip.c_str(), &dst) != 1) {
            std::cerr << "Invalid IPv6 address\n";
            close(fd);
            return 2;
        }
        std::memcpy(msg.req.id.idiag_src, &src, sizeof(src));
        std::memcpy(msg.req.id.idiag_dst, &dst, sizeof(dst));
    } else {
        std::cerr << "Unsupported address family\n";
        close(fd);
        return 2;
    }

    sockaddr_nl nladdr{};
    nladdr.nl_family = AF_NETLINK;

    iovec iov{};
    iov.iov_base = &msg;
    iov.iov_len = sizeof(msg);

    msghdr mh{};
    mh.msg_name = &nladdr;
    mh.msg_namelen = sizeof(nladdr);
    mh.msg_iov = &iov;
    mh.msg_iovlen = 1;

    if (sendmsg(fd, &mh, 0) < 0) {
        std::cerr << "Failed to send SOCK_DESTROY: " << std::strerror(errno) << "\n";
        close(fd);
        return 1;
    }

    char buf[8192];
    iovec riov{};
    riov.iov_base = buf;
    riov.iov_len = sizeof(buf);
    msghdr rmh{};
    rmh.msg_iov = &riov;
    rmh.msg_iovlen = 1;

    ssize_t len = recvmsg(fd, &rmh, 0);
    if (len < 0) {
        std::cerr << "Failed to receive netlink ack: " << std::strerror(errno) << "\n";
        close(fd);
        return 1;
    }

    for (nlmsghdr *h = reinterpret_cast<nlmsghdr *>(buf); NLMSG_OK(h, static_cast<unsigned int>(len)); h = NLMSG_NEXT(h, len)) {
        if (h->nlmsg_type == NLMSG_ERROR) {
            auto *err = reinterpret_cast<nlmsgerr *>(NLMSG_DATA(h));
            if (err->error == 0) {
                std::cout << "Socket destroyed\n";
                close(fd);
                return 0;
            }
            int saved_errno = -err->error;
            errno = saved_errno;
            std::cerr << "SOCK_DESTROY failed: " << std::strerror(errno) << "\n";
            close(fd);
            errno = saved_errno;
            return 1;
        }
    }

    std::cerr << "Unexpected netlink response\n";
    close(fd);
    return 1;
}

unsigned short csum16_finish(unsigned int sum) {
    while (sum >> 16) sum = (sum & 0xFFFFu) + (sum >> 16);
    return static_cast<unsigned short>(~sum);
}

unsigned short tcp_checksum_v4(const in_addr &src, const in_addr &dst, const tcphdr &tcp) {
    unsigned int sum = 0;
    const auto *s = reinterpret_cast<const unsigned char *>(&src);
    const auto *d = reinterpret_cast<const unsigned char *>(&dst);
    sum += (s[0] << 8) | s[1];
    sum += (s[2] << 8) | s[3];
    sum += (d[0] << 8) | d[1];
    sum += (d[2] << 8) | d[3];
    sum += IPPROTO_TCP;
    sum += sizeof(tcphdr);
    const auto *p = reinterpret_cast<const unsigned char *>(&tcp);
    for (size_t i = 0; i + 1 < sizeof(tcphdr); i += 2) {
        sum += (p[i] << 8) | p[i + 1];
    }
    if (sizeof(tcphdr) & 1) sum += p[sizeof(tcphdr) - 1] << 8;
    return htons(csum16_finish(sum));
}

bool sniff_tcp_seq_v4(const std::string &local_ip, unsigned short local_port,
                      const std::string &remote_ip, unsigned short remote_port,
                      uint32_t &seq, uint32_t &ack_seq, bool &seen) {
    seen = false;
    int fd = socket(AF_INET, SOCK_RAW, IPPROTO_TCP);
    if (fd < 0) { std::cerr << "RST sniff socket failed: " << std::strerror(errno) << "\n"; return false; }
    timeval tv{};
    tv.tv_sec = 2;
    tv.tv_usec = 0;
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    in_addr l{}, r{};
    inet_pton(AF_INET, local_ip.c_str(), &l);
    inet_pton(AF_INET, remote_ip.c_str(), &r);

    unsigned char buf[65536];
    for (int i = 0; i < 200; ++i) {
        ssize_t n = recv(fd, buf, sizeof(buf), 0);
        if (n < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) break;
            continue;
        }
        if (n < static_cast<ssize_t>(sizeof(iphdr))) continue;
        auto *ip = reinterpret_cast<iphdr *>(buf);
        if (ip->protocol != IPPROTO_TCP) continue;
        size_t ihl = static_cast<size_t>(ip->ihl) * 4;
        if (n < static_cast<ssize_t>(ihl + sizeof(tcphdr))) continue;
        auto *tcp = reinterpret_cast<tcphdr *>(buf + ihl);
        uint16_t sp = ntohs(tcp->source);
        uint16_t dp = ntohs(tcp->dest);
        if (ip->saddr == l.s_addr && ip->daddr == r.s_addr && sp == local_port && dp == remote_port) {
            seq = ntohl(tcp->seq);
            ack_seq = ntohl(tcp->ack_seq);
            seen = true;
            close(fd);
            return true;
        }
        if (ip->saddr == r.s_addr && ip->daddr == l.s_addr && sp == remote_port && dp == local_port) {
            // packet from remote -> local: to RST from local side, seq should be ack_seq of this packet
            seq = ntohl(tcp->ack_seq);
            ack_seq = ntohl(tcp->seq);
            seen = true;
            close(fd);
            return true;
        }
    }
    close(fd);
    return false;
}

int send_tcp_rst_v4(const std::string &src_ip, unsigned short src_port,
                    const std::string &dst_ip, unsigned short dst_port,
                    uint32_t seq, uint32_t ack_seq, bool ack) {
    int fd = socket(AF_INET, SOCK_RAW, IPPROTO_RAW);
    if (fd < 0) { std::cerr << "RST raw socket failed: " << std::strerror(errno) << "\n"; return -1; }
    int one = 1;
    setsockopt(fd, IPPROTO_IP, IP_HDRINCL, &one, sizeof(one));

    char packet[sizeof(iphdr) + sizeof(tcphdr)]{};
    auto *ip = reinterpret_cast<iphdr *>(packet);
    auto *tcp = reinterpret_cast<tcphdr *>(packet + sizeof(iphdr));

    in_addr src{}, dst{};
    inet_pton(AF_INET, src_ip.c_str(), &src);
    inet_pton(AF_INET, dst_ip.c_str(), &dst);

    ip->ihl = 5;
    ip->version = 4;
    ip->tos = 0;
    ip->tot_len = htons(sizeof(packet));
    ip->id = htons(0);
    ip->frag_off = 0;
    ip->ttl = 64;
    ip->protocol = IPPROTO_TCP;
    ip->saddr = src.s_addr;
    ip->daddr = dst.s_addr;
    ip->check = 0;

    tcp->source = htons(src_port);
    tcp->dest = htons(dst_port);
    tcp->seq = htonl(seq);
    tcp->ack_seq = htonl(ack_seq);
    tcp->doff = 5;
    tcp->rst = 1;
    tcp->ack = ack ? 1 : 0;
    tcp->window = htons(0);
    tcp->check = tcp_checksum_v4(src, dst, *tcp);

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(dst_port);
    addr.sin_addr = dst;
    ssize_t sent = sendto(fd, packet, sizeof(packet), 0, reinterpret_cast<sockaddr *>(&addr), sizeof(addr));
    int saved = errno;
    close(fd);
    errno = saved;
    return sent == static_cast<ssize_t>(sizeof(packet)) ? 0 : -1;
}

int kill_tcp_with_rst_v4(const std::string &local_ip, unsigned short local_port,
                         const std::string &remote_ip, unsigned short remote_port) {
    uint32_t seq = 0, ack_seq = 0;
    bool seen = false;
    bool sniff_ok = sniff_tcp_seq_v4(local_ip, local_port, remote_ip, remote_port, seq, ack_seq, seen);
    std::cerr << "RST sniff: " << (sniff_ok ? "seen" : (seen ? "seen-flag" : "not-seen"))
              << " seq=" << seq << " ack=" << ack_seq << "\n";
    int ok = 0;
    if (seen) {
        if (send_tcp_rst_v4(local_ip, local_port, remote_ip, remote_port, seq, 0, false) == 0) ok = 1;
        else std::cerr << "RST send local->remote failed: " << std::strerror(errno) << "\n";
        if (send_tcp_rst_v4(remote_ip, remote_port, local_ip, local_port, ack_seq, 0, false) == 0) ok = 1;
        else std::cerr << "RST send remote->local failed: " << std::strerror(errno) << "\n";
    } else {
        if (send_tcp_rst_v4(local_ip, local_port, remote_ip, remote_port, 0, 0, false) == 0) ok = 1;
        else std::cerr << "RST blind local->remote failed: " << std::strerror(errno) << "\n";
        if (send_tcp_rst_v4(remote_ip, remote_port, local_ip, local_port, 0, 0, false) == 0) ok = 1;
        else std::cerr << "RST blind remote->local failed: " << std::strerror(errno) << "\n";
    }
    return ok ? 0 : 1;
}

int kill_tcp_socket(const std::string &local_ip, unsigned short local_port,
                    const std::string &remote_ip, unsigned short remote_port) {
    uint8_t family = 0;
    if (detect_family(local_ip, remote_ip, family) != 0) {
        return 2;
    }

    int rc = kill_inet_socket(family, IPPROTO_TCP, local_ip, local_port, remote_ip, remote_port);
    if (rc == 0) return 0;

    if (errno == EOPNOTSUPP) {
        bool listen_like = (remote_port == 0);
        if (listen_like) {
            in_addr r4{};
            in6_addr r6{};
            if (family == AF_INET && inet_pton(AF_INET, remote_ip.c_str(), &r4) == 1)
                listen_like = (r4.s_addr == 0);
            else if (family == AF_INET6 && inet_pton(AF_INET6, remote_ip.c_str(), &r6) == 1)
                listen_like = IN6_IS_ADDR_UNSPECIFIED(&r6);
        }
        if (listen_like) {
            std::cerr << "SOCK_DESTROY unsupported; cannot kill LISTEN socket via TCP RST injection\n";
            return 1;
        }
        std::cerr << "SOCK_DESTROY unsupported, trying TCP RST injection...\n";
        if (family == AF_INET) {
            int rst = kill_tcp_with_rst_v4(local_ip, local_port, remote_ip, remote_port);
            if (rst == 0) {
                std::cout << "TCP RST sent\n";
                return 0;
            }
            std::cerr << "TCP RST injection failed\n";
            return 1;
        }
        std::cerr << "TCP RST injection fallback currently supports IPv4 only\n";
        return 1;
    }

    return rc;
}

int kill_udp_socket(const std::string &local_ip, unsigned short local_port,
                    const std::string &remote_ip, unsigned short remote_port) {
    uint8_t family = 0;
    if (detect_family(local_ip, remote_ip, family) != 0) {
        return 2;
    }
    return kill_inet_socket(family, IPPROTO_UDP, local_ip, local_port, remote_ip, remote_port);
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
