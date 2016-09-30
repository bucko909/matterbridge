import web
import sys
import simplejson

urls = (
  '/', 'index'
)

ircd = getattr(sys.modules['__main__'], 'ircd', None)

class index:
    def POST(self):
        input = web.input()
	print input
	channel = '#' + str(input.channel_name)
	user_name = input.user_name
	message = input.text
	chan = ircd.get_channel(channel)
	for client in chan.members:
		client.message(':%s PRIVMSG %s :%s' % (user_name, channel, message))
	return ""

channels = {
	'#off-topic': '', # hook URL here
}

import urllib2

import miniircd
old_message_channel = miniircd.Client.message_channel
def message_channel(self, channel, command, message, include_self=False):
	try:
		print "message_channel", channel, command, message, include_self
		if command == 'PRIVMSG' and channel.name in channels:
			url = channels[channel.name]
			print url
			resp = urllib2.urlopen(url, 'payload=' + simplejson.dumps({'text': message.split(' ', 1)[1][1:], 'username': self.nickname}))
			print "Fetched"
			print resp
			print list(resp)
		else:
			print channel.name, channels, command
		print "Done."
	except:
		import traceback
		traceback.print_exc()
	old_message_channel(self, channel, command, message)
miniircd.Client.message_channel = message_channel

app = web.application(urls, globals())

if __name__ == '__main__':
	import threading
	ht = threading.Thread(target=app.run)
	ht.setDaemon(True)
	ht.start()
	ircd = miniircd.Server()
	it = threading.Thread(target=ircd.run)
	it.setDaemon(True)
	it.start()
	import pdb
	pdb.set_trace()
