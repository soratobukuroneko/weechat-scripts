# TODO option ignore chan
import html
import re
import weechat
from socket import timeout
from urllib.error import URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

SCRIPT_NAME = "urltitel"
SCRIPT_AUTHOR = "soratobuneko"
SCRIPT_VERSION = "7"
SCRIPT_LICENCE = "WTFPL"
SCRIPT_DESCRIPTION = (
    "Display or send titles of URLs from incoming and outcoming messages. "
    + "Also features an optional URL buffer"
)
UA = f"Mozilla/5.0 (Python) weechat {SCRIPT_NAME}"

script_options = {
    "timeout": ("3", "Maximum time to wait to fetch URL."),
    "retry": ("off", "Retry fetching URL if it fails the first time."),
    "maxlength": ("200", "Maximum length of title."),
    "maxdownload": ("262144", "Maximum size (Bytes) to fetch from URL."),
    "serverchans": (
        "*,*",
        '"|" separated list of server,#channel to parse. for instance: "server0,#channel1|server0,#channel2"',
    ),
    "replyto": (
        "",
        '"|" separated list of server,#channel for which instead of displaying localy a message we send it to the channel.',
    ),
    "sendfromme": ("off", "Alway send titles for URLs sent by ourself."),
    "urlbuffer": ("off", "Create a buffer to collect the URLs with their titles."),
    "debug": ("off", "Show debug messages"),
}


def create_buffer():
    global url_buffer
    BUFFER_NAME = f"{SCRIPT_NAME}"
    url_buffer = weechat.buffer_new(BUFFER_NAME, "", "", "on_buffer_close", "")
    weechat.buffer_set(
        url_buffer, "title", f"URL buffer ({SCRIPT_NAME} v{SCRIPT_VERSION})"
    )


def debug(message):
    if script_options["debug"] == "on":
        weechat.prnt("", f"{SCRIPT_NAME}: {message}")


def error(message):
    weechat.prnt("", f"{weechat.prefix('error')}{SCRIPT_NAME}: {message}")


def fetch_html(url):
    # IRI to URL (unicode to ascii)
    url = urlsplit(url)
    url = list(url)
    url[1] = quote(url[1])  # URL encode domain
    url[2] = quote(url[2])  # URL encode path
    url = urlunsplit(url)
    request = Request(url, data=None, headers={"User-Agent": UA})

    tries = 2 if script_options["retry"] == "on" else 1
    for i in range(0, tries):
        try:
            with urlopen(request, timeout=int(script_options["timeout"])) as res:
                is_html = bool(re.match(".*/html.*", res.info()["Content-Type"]))
                if is_html:
                    debug(f"Got an HTML document. Reading at most {script_options['maxdownload']} bytes.")
                    html_doc_head = res.read(int(script_options["maxdownload"])).decode()
                    return html_doc_head
                else:
                    debug("Not an HTML document.")
                    return
        except URLError as err:
            error(f"Cannot fetch {url}. {err.reason}")
        except timeout:
            error(f"Socket timed out while fetching {url}")


_re_url = re.compile(r"https?://[\w0-9@:%._\+~#=()?&/\-]+")


def find_urls(message):
    # Found URLs with title [["http://perdu.com", "Vous Etes Perdu ?"], ...]
    # If URL point to a non HTML document the list element is None. If the
    # HTML doc has no <title> the list element is ["https://..", None]
    urls = []
    urls_count = 0

    if re.match(r"^url\|\d+\): ", message):
        return (0, ())

    if re.match(r"https?://[^ ]", message) and not re.match(_re_url, message):
        debug(f"Failling to match URL in message: {message}")

    for url in re.findall(_re_url, message):
        debug(f"Fetching title for URL: {url}")
        html = fetch_html(url)
        if html is not None:
            title = get_title(html)
            if title is not None and len(title):
                urls_count += 1
                debug(f"Found title: {title}")
                if len(title) > int(script_options["maxlength"]):
                    urls.append([url, title[0 : int(script_options["maxlength"])]])
                else:
                    urls.append([url, title])
        else:
            urls.append(None)

    return (urls_count, urls)


_re_whitespace = re.compile(r"\s")


def get_title(html_doc):
    title = re.search(r"(?i)<title ?[^<>]*>([^<>]*)</title>", html_doc)
    if title is None:
        debug("No <title> found.")
        return
    else:
        title = html.unescape(title.group(1))

    # many whitespaces to one space
    stripped_title = ""
    for i, char in enumerate(title):
        if not re.match(_re_whitespace, char):
            stripped_title += char
        elif i > 0 and not re.match(_re_whitespace, title[i - 1]):
            stripped_title += " "
    stripped_title = stripped_title.strip()

    return stripped_title


def on_config_change(data, option, value):
    key = option.split(".")[-1]
    script_options[key] = value
    return weechat.WEECHAT_RC_OK


def on_buffer_close(data, buffer):
    global url_buffer
    url_buffer = None
    return weechat.WEECHAT_RC_OK


def on_privmsg(data, signal, signal_data):
    global url_buffer
    server = signal.split(",")[0]
    msg = weechat.info_get_hashtable("irc_message_parse", {"message": signal_data})
    srvchan = f"{server},{msg['channel']}"

    # Parse only messages from configured server/channels
    if not srvchan_in_list(srvchan, script_options["serverchans"].split("|")):
        debug(f"Ignoring message from {server}/{msg['channel']}")
        return weechat.WEECHAT_RC_OK

    urls_found = find_urls(msg["text"])
    if script_options["urlbuffer"] == "on" and len(urls_found[1]):
        nick = msg["nick"]
        if not len(nick):
            nick = f"{weechat.color('*white')}{weechat.info_get('irc_nick', server)}{weechat.color('default')}"
        if not url_buffer:
            create_buffer()
        weechat.prnt(
            url_buffer,
            f"<{nick}{weechat.color('red')}@{weechat.color('default')}{server}/{msg['channel']}>\t{msg['text']}"
        )
    if urls_found[0]:
        force_send = (
            True
            if script_options["sendfromme"] == "on" and not len(msg["nick"])
            else False
        )
        show_urls_title(srvchan, urls_found[1], force_send)

    return weechat.WEECHAT_RC_OK


def show_urls_title(srvchan, urls, force_send):
    ACTION_SEND = "Sending"
    buffer = weechat.info_get("irc_buffer", srvchan)
    action = (
        (ACTION_SEND, "to")
        if force_send or srvchan_in_list(srvchan, script_options["replyto"].split("|"))
        else ("Displaying", "on")
    )
    if buffer:
        for i, url in enumerate(urls):
            if url is not None:
                debug(f"{action[0]} title(s) {action[1]} {srvchan}")
                if action[0] == ACTION_SEND:
                    weechat.command(buffer, f"url|{i + 1}): {url[1]}")
                else:  # We have already checked script_options["serverchans"] in on_privmsg
                    weechat.prnt(buffer, f"{i + 1}:\t{url[1]}")
                if script_options["urlbuffer"] == "on":
                    if url_buffer is None:
                        create_buffer()
                    weechat.prnt(url_buffer, f"{i + 1}:\t{url[1]}")


def srvchan_in_list(srvchan, srvchan_list):
    srvchan = srvchan.lower().split(",")
    for _srvchan in srvchan_list:
        _srvchan = _srvchan.lower().split(",")
        if (_srvchan[0] == "*" or srvchan[0] == _srvchan[0]) and (
            _srvchan[1] == "*" or srvchan[1] == _srvchan[1]
        ):
            return True
    return False


weechat.register(
    SCRIPT_NAME,
    SCRIPT_AUTHOR,
    SCRIPT_VERSION,
    SCRIPT_LICENCE,
    SCRIPT_DESCRIPTION,
    "",
    "",
)

for option, default_value in list(script_options.items()):
    if not weechat.config_is_set_plugin(option):
        weechat.config_set_plugin(option, default_value[0])
        script_options[option] = default_value[0]
    else:
        script_options[option] = weechat.config_get_plugin(option)
    weechat.config_set_desc_plugin(
        option, f"{default_value[1]} (default: {default_value[0]})"
    )

if script_options["urlbuffer"] == "on":
    create_buffer()


weechat.hook_config("plugins.var.python." + SCRIPT_NAME + ".*", "on_config_change", "")
weechat.hook_signal("*,irc_in2_privmsg", "on_privmsg", "")
weechat.hook_signal("*,irc_out1_privmsg", "on_privmsg", "")
