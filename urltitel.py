import html
import json
import re
from collections import defaultdict
from socket import timeout
from typing import DefaultDict, List, NamedTuple, Optional
from urllib.error import URLError
from urllib.parse import ParseResult, quote, urlparse, urlunparse
from urllib.request import Request, urlopen
import weechat

SCRIPT_NAME = "urltitel"
SCRIPT_AUTHOR = "soratobuneko"
SCRIPT_VERSION = "9"
SCRIPT_LICENCE = "WTFPL"
SCRIPT_DESCRIPTION = (
    "Display or send titles of URLs from incoming and outcoming messages. "
    + "Also features an optional URL buffer"
)
UA = f"Mozilla/5.0 (Python) weechat {SCRIPT_NAME}"
BUFFER_NAME = SCRIPT_NAME

OPTIONS = {
    "debug": ("off",
              "Show debug messages"),
    "http_rewrite": ("on",
                     "Rewrite HTTP URL to HTTPS"),
    "maxdownload": ("262144",
                    "Maximum size (Bytes) to fetch from URL."),
    "maxlength": ("200",
                  "Maximum length of title."),
    "replyto": ("",
                '"|" separated list of server,#channel for which instead of displaying localy a message we send it to the channel.'),
    "retry": ("off",
              "Retry fetching URL if it fails the first time."),
    "sendfromme": ("off",
                   "Always send titles for URLs sent by ourself."),
    "serverchans": ("*,*",
                    '"|" separated list of server,#channel to parse. for instance: "server0,#channel1|server0,#channel2"'),
    "timeout": ("3",
                "Maximum time to wait to fetch URL."),
    "urlbuffer": ("off",
                  "Create a buffer to collect the URLs with their titles."),
}

script_options: DefaultDict[str, str] = defaultdict()

url_buffer = None

def create_buffer() -> None:
    global url_buffer
    url_buffer = weechat.buffer_new(BUFFER_NAME, "", "", "on_buffer_close", "")
    weechat.buffer_set(
        url_buffer, "title", f"URL buffer ({SCRIPT_NAME} v{SCRIPT_VERSION})"
    )


def debug(message: str) -> None:
    if script_options["debug"] == "on":
        weechat.prnt("", f"{SCRIPT_NAME}: {message}")


def error(message: str) -> None:
    weechat.prnt("", f"{weechat.prefix('error')}{SCRIPT_NAME}: {message}")


class Document(NamedTuple):
    url: ParseResult
    src: str

def fetch_html(url: str) -> Optional[Document]:
    # IRI to URL (unicode to ascii)
    url_parsed = urlparse(url)
    url_urlencoded = ParseResult(
        scheme=("https" if script_options["http_rewrite"]
                and url_parsed.scheme == "http"
                else url_parsed.scheme),
        netloc=quote(url_parsed.netloc),
        path=quote(url_parsed.path),
        params=url_parsed.params,
        query=url_parsed.query,
        fragment=url_parsed.fragment
    )
    url = urlunparse(url_urlencoded)
    request = Request(url, data=None, headers={"User-Agent": UA})

    tries = 2 if script_options["retry"] == "on" else 1
    for i in range(0, tries):
        try:
            with urlopen(request, timeout=int(script_options["timeout"])) as res:
                is_html = bool(re.match(".*/html.*", res.info()["Content-Type"]))
                if is_html:
                    debug(f"Got an HTML document. Reading at most {script_options['maxdownload']} bytes.")
                    html_doc_head = res.read(int(script_options["maxdownload"])).decode(errors="ignore")
                    return Document(url=url_urlencoded, src=html_doc_head)
                else:
                    debug("Not an HTML document.")
                    return None
        except URLError as err:
            error(f"Cannot fetch {url}. {err.reason}")
        except timeout:
            error(f"Socket timed out while fetching {url}")

    return None


_re_url = re.compile(r"https?://[\w0-9@:%._\+~#=()?&/\-]+")


def find_urls(message: str) -> List[str]:
    if re.match(r"^url\|\d+\): ", message):
        return []

    return re.findall(_re_url, message)


_re_whitespace = re.compile(r"\s")


def get_title(html_doc: Document) -> Optional[str]:
    title = None
    title_match = re.search(r"(?i)<title ?[^<>]*>([^<>]*)</title>",
                            html_doc.src)
    if title_match is None:
        debug("No <title> found.")
        return None
    else:
        title = html.unescape(title_match.group(1))

    # many whitespaces to one space
    stripped_title = ""
    for i, char in enumerate(title):
        if not re.match(_re_whitespace, char):
            stripped_title += char
        elif i > 0 and not re.match(_re_whitespace, title[i - 1]):
            stripped_title += " "
    stripped_title = stripped_title.strip()

    if stripped_title.find("The Pirate Bay - The galaxy's most resilient bittorrent site") == 0:
        torrent = tpb_get_torrent_by_url(html_doc.url)
        if torrent is not None:
            stripped_title = f"TPB torrent: {torrent.name}"

    return stripped_title

class Torrent(NamedTuple):
    id: int
    name: str

def tpb_get_torrent(id: int) -> Torrent:
    request = Request(f"https://apibay.org/t.php?id={id}",
                      data=None,
                      headers={"User-Agent": UA})
    with urlopen(url=request,
                 timeout=int(script_options["timeout"])) as response:
        json_ = json.load(response)
        return Torrent(id=id, name=json_["name"])


_re_query_id = re.compile(r"^(?:[^&]*[&])*id=([0-9]+)$(?:[&][^&]*)*")



def tpb_get_torrent_by_url(url: ParseResult) -> Optional[Torrent]:
    if url.path.endswith("description.php"):
        id_match = re.match(_re_query_id, url.query)
        return (tpb_get_torrent(id=int(id_match.group(1)))
                if id_match is not None
                else None)
    return None

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
    msg = weechat.info_get_hashtable("irc_message_parse",
                                     {"message": signal_data})
    srvchan = f"{server},{msg['channel']}"

    # Parse only messages from configured server/channels
    if not srvchan_in_list(srvchan, script_options["serverchans"].split("|")):
        debug(f"Ignoring message from {server}/{msg['channel']}")
        return weechat.WEECHAT_RC_OK

    urls = find_urls(msg["text"])
    titles = []
    for url in urls:
        debug(f"Fetching title for {url}")
        html_doc = fetch_html(url)
        if html_doc is not None:
            title = get_title(html_doc)
            if title is not None and len(title) > 0:
                if len(title) > int(script_options["maxlength"]):
                    title = title[0: int(script_options["maxlength"])] + "…"
                debug(f"Found title: {title}")
            titles.append(title)
        else:
            titles.append(None)

    if len(urls) > 0:
        if script_options["urlbuffer"] == "on":
            nick = msg["nick"]
            if len(nick) == 0:
                nick = f"{weechat.color('*white')}{weechat.info_get('irc_nick', server)}{weechat.color('default')}"
            if not url_buffer:
                create_buffer()
            weechat.prnt(
                url_buffer,
                f"<{nick}{weechat.color('red')}@{weechat.color('default')}{server}/{msg['channel']}>\t{msg['text']}",
            )
        force_send = (script_options["sendfromme"] == "on"
                      and len(msg["nick"]) == 0)
        show_urls_title(srvchan, titles, force_send)

    return weechat.WEECHAT_RC_OK


def show_urls_title(srvchan: str, titles: List[str], force_send: bool) -> None:
    ACTION_SEND = "Sending"
    buffer = weechat.info_get("irc_buffer", srvchan)
    action = (
        (ACTION_SEND, "to")
        if force_send or srvchan_in_list(srvchan,
                                         script_options["replyto"].split("|"))
        else ("Displaying", "on")
    )
    if buffer is not None:
        for i, title in enumerate(titles):
            if title is not None:
                debug(f"{action[0]} title(s) {action[1]} {srvchan}")
                if action[0] == ACTION_SEND:
                    weechat.command(buffer, f"url|{i + 1}): {title}")
                else:  # We have already checked script_options["serverchans"] in on_privmsg
                    weechat.prnt(buffer, f"{i + 1}:\t{title}")
                if script_options["urlbuffer"] == "on":
                    if url_buffer is None:
                        create_buffer()
                    weechat.prnt(url_buffer, f"{i + 1}:\t{title}")


def srvchan_in_list(srvchan: str, srvchan_list: List[str]) -> bool:
    srv_chan = srvchan.lower().split(",")
    for _srvchan in srvchan_list:
        _srv_chan = _srvchan.lower().split(",")
        if (_srv_chan[0] == "*" or srv_chan[0] == _srv_chan[0]) and (
            _srv_chan[1] == "*" or srv_chan[1] == _srv_chan[1]
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

for option, default_value in list(OPTIONS.items()):
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


weechat.hook_config("plugins.var.python." + SCRIPT_NAME + ".*",
                    "on_config_change", "")
weechat.hook_signal("*,irc_in2_privmsg", "on_privmsg", "")
weechat.hook_signal("*,irc_out1_privmsg", "on_privmsg", "")
