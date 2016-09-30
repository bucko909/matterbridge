#! /usr/bin/env python
# Hey, Emacs! This is -*-python-*-.
#
# Copyright (C) 2003-2015 Joel Rosdahl
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307
# USA
#
# Joel Rosdahl <joel@rosdahl.net>

VERSION = "1.1"

import re
import select
import socket
import string
import sys
import time
from datetime import datetime


class Channel(object):
    def __init__(self, server, name):
        self.server = server
        self.name = name
        self.members = set()
        self._topic = ""
        self._key = None

    def add_member(self, client):
        self.members.add(client)

    def get_topic(self):
        return self._topic

    def set_topic(self, value):
        self._topic = value

    topic = property(get_topic, set_topic)

    def get_key(self):
        return self._key

    def set_key(self, value):
        self._key = value

    key = property(get_key, set_key)

    def remove_client(self, client):
        self.members.discard(client)
        if not self.members:
            self.server.remove_channel(self)

class Client(object):
    __linesep_regexp = re.compile(r"\r?\n")
    # The RFC limit for nicknames is 9 characters, but what the heck.
    __valid_nickname_regexp = re.compile(
        r"^[][\`_^{|}A-Za-z][][\`_^{|}A-Za-z0-9-]{0,50}$")
    __valid_channelname_regexp = re.compile(
        r"^[&#+!][^\x00\x07\x0a\x0d ,:]{0,50}$")

    def __init__(self, server, socket):
        self.server = server
        self.socket = socket
        self.channels = {}  # irc_lower(Channel name) --> Channel
        self.nickname = None
        self.user = None
        self.realname = None
        (self.host, self.port) = socket.getpeername()
        self.__timestamp = time.time()
        self.__readbuffer = ""
        self.__writebuffer = ""
        self.__sent_ping = False
	self.ready_to_write = False
        if self.server.password:
            self.__handle_command = self.__pass_handler
        else:
            self.__handle_command = self.__registration_handler

    def get_prefix(self):
        return "%s!%s@%s" % (self.nickname, self.user, self.host)
    prefix = property(get_prefix)

    def check_aliveness(self):
        now = time.time()
        if self.__timestamp + 180 < now:
            self.disconnect("ping timeout")
            return
        if not self.__sent_ping and self.__timestamp + 90 < now:
            if self.__handle_command == self.__command_handler:
                # Registered.
                self.message("PING :%s" % self.server.name)
                self.__sent_ping = True
            else:
                # Not registered.
                self.disconnect("ping timeout")

    def write_queue_size(self):
        return len(self.__writebuffer)

    def __parse_read_buffer(self):
        lines = self.__linesep_regexp.split(self.__readbuffer)
        self.__readbuffer = lines[-1]
        lines = lines[:-1]
        for line in lines:
            if not line:
                # Empty line. Ignore.
                continue
            x = line.split(" ", 1)
            command = x[0].upper()
            if len(x) == 1:
                arguments = []
            else:
                if len(x[1]) > 0 and x[1][0] == ":":
                    arguments = [x[1][1:]]
                else:
                    y = string.split(x[1], " :", 1)
                    arguments = string.split(y[0])
                    if len(y) == 2:
                        arguments.append(y[1])
            self.__handle_command(command, arguments)

    def __pass_handler(self, command, arguments):
        server = self.server
        if command == "PASS":
            if len(arguments) == 0:
                self.reply_461("PASS")
            else:
                if arguments[0].lower() == server.password:
                    self.__handle_command = self.__registration_handler
                else:
                    self.reply("464 :Password incorrect")
        elif command == "QUIT":
            self.disconnect("Client quit")
            return

    def __registration_handler(self, command, arguments):
        server = self.server
        if command == "NICK":
            if len(arguments) < 1:
                self.reply("431 :No nickname given")
                return
            nick = arguments[0]
            if server.get_client(nick):
                self.reply("433 * %s :Nickname is already in use" % nick)
            elif not self.__valid_nickname_regexp.match(nick):
                self.reply("432 * %s :Erroneous nickname" % nick)
            else:
                self.nickname = nick
                server.client_changed_nickname(self, None)
        elif command == "USER":
            if len(arguments) < 4:
                self.reply_461("USER")
                return
            self.user = arguments[0]
            self.realname = arguments[3]
        elif command == "QUIT":
            self.disconnect("Client quit")
            return
        if self.nickname and self.user:
            self.reply("001 %s :Hi, welcome to IRC" % self.nickname)
            self.reply("002 %s :Your host is %s, running version miniircd-%s"
                       % (self.nickname, server.name, VERSION))
            self.reply("003 %s :This server was created sometime"
                       % self.nickname)
            self.reply("004 %s :%s miniircd-%s o o"
                       % (self.nickname, server.name, VERSION))
            self.send_lusers()
            self.send_motd()
            self.__handle_command = self.__command_handler

    def __command_handler(self, command, arguments):
        def away_handler():
            pass

        def ison_handler():
            if len(arguments) < 1:
                self.reply_461("ISON")
                return
            nicks = arguments
            online = [n for n in nicks if server.get_client(n)]
            self.reply("303 %s :%s" % (self.nickname, " ".join(online)))

        def join_handler():
            if len(arguments) < 1:
                self.reply_461("JOIN")
                return
            if arguments[0] == "0":
                for (channelname, channel) in self.channels.items():
                    self.message_channel(channel, "PART", channelname, True)
                    self.channel_log(channel, "left", meta=True)
                    server.remove_member_from_channel(self, channelname)
                self.channels = {}
                return
            channelnames = arguments[0].split(",")
            if len(arguments) > 1:
                keys = arguments[1].split(",")
            else:
                keys = []
            keys.extend((len(channelnames) - len(keys)) * [None])
            for (i, channelname) in enumerate(channelnames):
                if irc_lower(channelname) in self.channels:
                    continue
                if not valid_channel_re.match(channelname):
                    self.reply_403(channelname)
                    continue
                channel = server.get_channel(channelname)
                if channel.key is not None and channel.key != keys[i]:
                    self.reply(
                        "475 %s %s :Cannot join channel (+k) - bad key"
                        % (self.nickname, channelname))
                    continue
                channel.add_member(self)
                self.channels[irc_lower(channelname)] = channel
                self.message_channel(channel, "JOIN", channelname, True)
                self.channel_log(channel, "joined", meta=True)
                if channel.topic:
                    self.reply("332 %s %s :%s"
                               % (self.nickname, channel.name, channel.topic))
                else:
                    self.reply("331 %s %s :No topic is set"
                               % (self.nickname, channel.name))
                self.reply("353 %s = %s :%s"
                           % (self.nickname,
                              channelname,
                              " ".join(sorted(x.nickname
                                              for x in channel.members))))
                self.reply("366 %s %s :End of NAMES list"
                           % (self.nickname, channelname))

        def list_handler():
            if len(arguments) < 1:
                channels = server.channels.values()
            else:
                channels = []
                for channelname in arguments[0].split(","):
                    if server.has_channel(channelname):
                        channels.append(server.get_channel(channelname))
            channels.sort(key=lambda x: x.name)
            for channel in channels:
                self.reply("322 %s %s %d :%s"
                           % (self.nickname, channel.name,
                              len(channel.members), channel.topic))
            self.reply("323 %s :End of LIST" % self.nickname)

        def lusers_handler():
            self.send_lusers()

        def mode_handler():
            if len(arguments) < 1:
                self.reply_461("MODE")
                return
            targetname = arguments[0]
            if server.has_channel(targetname):
                channel = server.get_channel(targetname)
                if len(arguments) < 2:
                    if channel.key:
                        modes = "+k"
                        if irc_lower(channel.name) in self.channels:
                            modes += " %s" % channel.key
                    else:
                        modes = "+"
                    self.reply("324 %s %s %s"
                               % (self.nickname, targetname, modes))
                    return
                flag = arguments[1]
                if flag == "+k":
                    if len(arguments) < 3:
                        self.reply_461("MODE")
                        return
                    key = arguments[2]
                    if irc_lower(channel.name) in self.channels:
                        channel.key = key
                        self.message_channel(
                            channel, "MODE", "%s +k %s" % (channel.name, key),
                            True)
                        self.channel_log(
                            channel, "set channel key to %s" % key, meta=True)
                    else:
                        self.reply("442 %s :You're not on that channel"
                                   % targetname)
                elif flag == "-k":
                    if irc_lower(channel.name) in self.channels:
                        channel.key = None
                        self.message_channel(
                            channel, "MODE", "%s -k" % channel.name,
                            True)
                        self.channel_log(
                            channel, "removed channel key", meta=True)
                    else:
                        self.reply("442 %s :You're not on that channel"
                                   % targetname)
                else:
                    self.reply("472 %s %s :Unknown MODE flag"
                               % (self.nickname, flag))
            elif targetname == self.nickname:
                if len(arguments) == 1:
                    self.reply("221 %s +" % self.nickname)
                else:
                    self.reply("501 %s :Unknown MODE flag" % self.nickname)
            else:
                self.reply_403(targetname)

        def motd_handler():
            self.send_motd()

        def nick_handler():
            if len(arguments) < 1:
                self.reply("431 :No nickname given")
                return
            newnick = arguments[0]
            client = server.get_client(newnick)
            if newnick == self.nickname:
                pass
            elif client and client is not self:
                self.reply("433 %s %s :Nickname is already in use"
                           % (self.nickname, newnick))
            elif not self.__valid_nickname_regexp.match(newnick):
                self.reply("432 %s %s :Erroneous Nickname"
                           % (self.nickname, newnick))
            else:
                for x in self.channels.values():
                    self.channel_log(
                        x, "changed nickname to %s" % newnick, meta=True)
                oldnickname = self.nickname
                self.nickname = newnick
                server.client_changed_nickname(self, oldnickname)
                self.message_related(
                    ":%s!%s@%s NICK %s"
                    % (oldnickname, self.user, self.host, self.nickname),
                    True)

        def notice_and_privmsg_handler():
            if len(arguments) == 0:
                self.reply("411 %s :No recipient given (%s)"
                           % (self.nickname, command))
                return
            if len(arguments) == 1:
                self.reply("412 %s :No text to send" % self.nickname)
                return
            targetname = arguments[0]
            message = arguments[1]
            client = server.get_client(targetname)
            if client:
                client.message(":%s %s %s :%s"
                               % (self.prefix, command, targetname, message))
            elif server.has_channel(targetname):
                channel = server.get_channel(targetname)
                self.message_channel(
                    channel, command, "%s :%s" % (channel.name, message))
                self.channel_log(channel, message)
            else:
                self.reply("401 %s %s :No such nick/channel"
                           % (self.nickname, targetname))

        def part_handler():
            if len(arguments) < 1:
                self.reply_461("PART")
                return
            if len(arguments) > 1:
                partmsg = arguments[1]
            else:
                partmsg = self.nickname
            for channelname in arguments[0].split(","):
                if not valid_channel_re.match(channelname):
                    self.reply_403(channelname)
                elif not irc_lower(channelname) in self.channels:
                    self.reply("442 %s %s :You're not on that channel"
                               % (self.nickname, channelname))
                else:
                    channel = self.channels[irc_lower(channelname)]
                    self.message_channel(
                        channel, "PART", "%s :%s" % (channelname, partmsg),
                        True)
                    self.channel_log(channel, "left (%s)" % partmsg, meta=True)
                    del self.channels[irc_lower(channelname)]
                    server.remove_member_from_channel(self, channelname)

        def ping_handler():
            if len(arguments) < 1:
                self.reply("409 %s :No origin specified" % self.nickname)
                return
            self.reply("PONG %s :%s" % (server.name, arguments[0]))

        def pong_handler():
            pass

        def quit_handler():
            if len(arguments) < 1:
                quitmsg = self.nickname
            else:
                quitmsg = arguments[0]
            self.disconnect(quitmsg)

        def topic_handler():
            if len(arguments) < 1:
                self.reply_461("TOPIC")
                return
            channelname = arguments[0]
            channel = self.channels.get(irc_lower(channelname))
            if channel:
                if len(arguments) > 1:
                    newtopic = arguments[1]
                    channel.topic = newtopic
                    self.message_channel(
                        channel, "TOPIC", "%s :%s" % (channelname, newtopic),
                        True)
                    self.channel_log(
                        channel, "set topic to %r" % newtopic, meta=True)
                else:
                    if channel.topic:
                        self.reply("332 %s %s :%s"
                                   % (self.nickname, channel.name,
                                      channel.topic))
                    else:
                        self.reply("331 %s %s :No topic is set"
                                   % (self.nickname, channel.name))
            else:
                self.reply("442 %s :You're not on that channel" % channelname)

        def wallops_handler():
            if len(arguments) < 1:
                self.reply_461(command)
            message = arguments[0]
            for client in server.clients.values():
                client.message(":%s NOTICE %s :Global notice: %s"
                               % (self.prefix, client.nickname, message))

        def who_handler():
            if len(arguments) < 1:
                return
            targetname = arguments[0]
            if server.has_channel(targetname):
                channel = server.get_channel(targetname)
                for member in channel.members:
                    self.reply("352 %s %s %s %s %s %s H :0 %s"
                               % (self.nickname, targetname, member.user,
                                  member.host, server.name, member.nickname,
                                  member.realname))
                self.reply("315 %s %s :End of WHO list"
                           % (self.nickname, targetname))

        def whois_handler():
            if len(arguments) < 1:
                return
            username = arguments[0]
            user = server.get_client(username)
            if user:
                self.reply("311 %s %s %s %s * :%s"
                           % (self.nickname, user.nickname, user.user,
                              user.host, user.realname))
                self.reply("312 %s %s %s :%s"
                           % (self.nickname, user.nickname, server.name,
                              server.name))
                self.reply("319 %s %s :%s"
                           % (self.nickname, user.nickname,
                              " ".join(user.channels)))
                self.reply("318 %s %s :End of WHOIS list"
                           % (self.nickname, user.nickname))
            else:
                self.reply("401 %s %s :No such nick"
                           % (self.nickname, username))

        handler_table = {
            "AWAY": away_handler,
            "ISON": ison_handler,
            "JOIN": join_handler,
            "LIST": list_handler,
            "LUSERS": lusers_handler,
            "MODE": mode_handler,
            "MOTD": motd_handler,
            "NICK": nick_handler,
            "NOTICE": notice_and_privmsg_handler,
            "PART": part_handler,
            "PING": ping_handler,
            "PONG": pong_handler,
            "PRIVMSG": notice_and_privmsg_handler,
            "QUIT": quit_handler,
            "TOPIC": topic_handler,
            "WALLOPS": wallops_handler,
            "WHO": who_handler,
            "WHOIS": whois_handler,
        }
        server = self.server
        valid_channel_re = self.__valid_channelname_regexp
        try:
            handler_table[command]()
        except KeyError:
            self.reply("421 %s %s :Unknown command" % (self.nickname, command))

    def socket_readable_notification(self):
        try:
            data = self.socket.recv(2 ** 10)
            self.server.print_debug(
                "[%s:%d] -> %r" % (self.host, self.port, data))
            quitmsg = "EOT"
        except socket.error as x:
            data = ""
            quitmsg = x
        if data:
            self.__readbuffer += data
            self.__parse_read_buffer()
            self.__timestamp = time.time()
            self.__sent_ping = False
        else:
            self.disconnect(quitmsg)

    def socket_writable_notification(self):
	if self.__writebuffer == '':
	    self.ready_to_write = True
	    return
	self.ready_to_write = False
        try:
            sent = self.socket.send(self.__writebuffer)
            self.server.print_debug(
                "[%s:%d] <- %r" % (
                    self.host, self.port, self.__writebuffer[:sent]))
            self.__writebuffer = self.__writebuffer[sent:]
	    if self.__writebuffer == '':
		self.ready_to_write = True
        except socket.error as x:
            self.disconnect(x)

    def disconnect(self, quitmsg):
        self.message("ERROR :%s" % quitmsg)
        self.server.print_info(
            "Disconnected connection from %s:%s (%s)." % (
                self.host, self.port, quitmsg))
        self.socket.close()
        self.server.remove_client(self, quitmsg)

    def message(self, msg):
        self.__writebuffer += msg + "\r\n"
	if self.ready_to_write:
		self.socket_writable_notification()

    def reply(self, msg):
        self.message(":%s %s" % (self.server.name, msg))

    def reply_403(self, channel):
        self.reply("403 %s %s :No such channel" % (self.nickname, channel))

    def reply_461(self, command):
        nickname = self.nickname or "*"
        self.reply("461 %s %s :Not enough parameters" % (nickname, command))

    def message_channel(self, channel, command, message, include_self=False):
        line = ":%s %s %s" % (self.prefix, command, message)
        for client in channel.members:
            if client != self or include_self:
                client.message(line)

    def channel_log(self, channel, message, meta=False):
	pass

    def message_related(self, msg, include_self=False):
        clients = set()
        if include_self:
            clients.add(self)
        for channel in self.channels.values():
            clients |= channel.members
        if not include_self:
            clients.discard(self)
        for client in clients:
            client.message(msg)

    def send_lusers(self):
        self.reply("251 %s :There are %d users and 0 services on 1 server"
                   % (self.nickname, len(self.server.clients)))

    def send_motd(self):
        server = self.server
        motdlines = server.get_motd_lines()
        if motdlines:
            self.reply("375 %s :- %s Message of the day -"
                       % (self.nickname, server.name))
            for line in motdlines:
                self.reply("372 %s :- %s" % (self.nickname, line.rstrip()))
            self.reply("376 %s :End of /MOTD command" % self.nickname)
        else:
            self.reply("422 %s :MOTD File is missing" % self.nickname)


class Server(object):
    def __init__(self, listen_host="", ports=(8001,), password=None, motd=(), verbose=True, debug=False):
        self.ports = ports
        self.password = password
        self.motd = motd
        self.verbose = verbose
        self.debug = debug

        if listen_host:
            self.address = socket.gethostbyname(listen_host)
        else:
            self.address = ""
        server_name_limit = 63  # From the RFC.
        self.name = socket.getfqdn(self.address)[:server_name_limit]

        self.channels = {}  # irc_lower(Channel name) --> Channel instance.
        self.clients = {}  # Socket --> Client instance.
        self.nicknames = {}  # irc_lower(Nickname) --> Client instance.

    def get_client(self, nickname):
        return self.nicknames.get(irc_lower(nickname))

    def has_channel(self, name):
        return irc_lower(name) in self.channels

    def get_channel(self, channelname):
        if irc_lower(channelname) in self.channels:
            channel = self.channels[irc_lower(channelname)]
        else:
            channel = Channel(self, channelname)
            self.channels[irc_lower(channelname)] = channel
        return channel

    def get_motd_lines(self):
	return self.motd

    def print_info(self, msg):
        if self.verbose:
            print(msg)
            sys.stdout.flush()

    def print_debug(self, msg):
        if self.debug:
            print(msg)
            sys.stdout.flush()

    def print_error(self, msg):
        sys.stderr.write("%s\n" % msg)

    def client_changed_nickname(self, client, oldnickname):
        if oldnickname:
            del self.nicknames[irc_lower(oldnickname)]
        self.nicknames[irc_lower(client.nickname)] = client

    def remove_member_from_channel(self, client, channelname):
        if irc_lower(channelname) in self.channels:
            channel = self.channels[irc_lower(channelname)]
            channel.remove_client(client)

    def remove_client(self, client, quitmsg):
        client.message_related(":%s QUIT :%s" % (client.prefix, quitmsg))
        for x in client.channels.values():
            client.channel_log(x, "quit (%s)" % quitmsg, meta=True)
            x.remove_client(client)
        if client.nickname \
                and irc_lower(client.nickname) in self.nicknames:
            del self.nicknames[irc_lower(client.nickname)]
        del self.clients[client.socket]

    def remove_channel(self, channel):
        del self.channels[irc_lower(channel.name)]

    def run(self):
        serversockets = []
        for port in self.ports:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((self.address, port))
            except socket.error as e:
                self.print_error("Could not bind port %s: %s." % (port, e))
                raise
            s.listen(5)
            serversockets.append(s)
            del s
            self.print_info("Listening on port %d." % port)
        last_aliveness_check = time.time()
        while True:
            (iwtd, owtd, ewtd) = select.select(
                serversockets + [x.socket for x in self.clients.values()],
                [x.socket for x in self.clients.values()
                 if x.write_queue_size() > 0],
                [],
                10)
            for x in iwtd:
                if x in self.clients:
                    self.clients[x].socket_readable_notification()
                else:
                    (conn, addr) = x.accept()
                    try:
                        self.clients[conn] = Client(self, conn)
                        self.print_info("Accepted connection from %s:%s." % (
                            addr[0], addr[1]))
                    except socket.error as e:
                        try:
                            conn.close()
                        except:
                            pass
            for x in owtd:
                if x in self.clients:  # client may have been disconnected
                    self.clients[x].socket_writable_notification()
            now = time.time()
            if last_aliveness_check + 10 < now:
                for client in self.clients.values():
                    client.check_aliveness()
                last_aliveness_check = now

_maketrans = str.maketrans if sys.version_info[0] == 3 else string.maketrans
_ircstring_translation = _maketrans(
    string.ascii_lowercase.upper() + "[]\\^",
    string.ascii_lowercase + "{}|~")


def irc_lower(s):
    return string.translate(s, _ircstring_translation)
