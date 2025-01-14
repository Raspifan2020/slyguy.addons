import threading
import os
import shutil
import re
import uuid
import time
import base64
import json

from io import BytesIO
from xml.dom.minidom import parseString
from collections import defaultdict
from functools import cmp_to_key

from six.moves.BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
from six.moves.socketserver import ThreadingMixIn
from six.moves.urllib.parse import urlparse, urljoin, unquote, parse_qsl, quote_plus
from kodi_six import xbmc, xbmcvfs
from requests import ConnectionError

from slyguy.log import log
from slyguy.constants import *
from slyguy.util import check_port, remove_file, get_kodi_string, set_kodi_string, fix_url
from slyguy.exceptions import Exit
from slyguy import settings, gui
from slyguy.session import RawSession
from slyguy.language import _
from slyguy.router import add_url_args

from .constants import *

#ADDON_DEV = True

REMOVE_IN_HEADERS = ['upgrade', 'host']
REMOVE_OUT_HEADERS = ['date', 'server', 'transfer-encoding']

PROXY_PORT = check_port(PROXY_PORT)
if not PROXY_PORT:
    PROXY_PORT = check_port()

PROXY_PATH = 'http://{}:{}/'.format(PROXY_HOST, PROXY_PORT)
settings.set('proxy_path', PROXY_PATH)

CODECS = {
    'avc': 'H.264',
    'hvc': 'H.265',
    'hev': 'H.265',
    'mp4v': 'MPEG-4',
    'mp4s': 'MPEG-4',
    'dvh': 'H.265 DV',
}

CODEC_RANKING = ['MPEG-4', 'H.264', 'H.265', 'H.265 DV']

PROXY_GLOBAL = {
    'last_quality': QUALITY_BEST,
    'session': {},
}

class RequestHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def __init__(self, request, client_address, server):
        try:
            BaseHTTPRequestHandler.__init__(self, request, client_address, server)
        except (IOError, OSError) as e:
            pass

    def log_message(self, format, *args):
        return

    def setup(self):
        BaseHTTPRequestHandler.setup(self)
        self.request.settimeout(5)

    def _get_url(self):
        url = self.path.lstrip('/').strip('\\')

        self._headers = {}
        self._plugin_headers = {}
        for header in self.headers:
            if header.lower() not in REMOVE_IN_HEADERS:
                self._headers[header.lower()] = self.headers[header]

        self._headers['accept-encoding'] = 'gzip, deflate, br'

        length = int(self._headers.get('content-length', 0))
        self._post_data = self.rfile.read(length) if length else None

        self._session = PROXY_GLOBAL['session']

        try:
            proxy_data = json.loads(get_kodi_string('_slyguy_quality'))
            if self._session.get('session_id') != proxy_data['session_id']:
                self._session = {}

            self._session.update(proxy_data)
            set_kodi_string('_slyguy_quality', '')
        except:
            pass

        PROXY_GLOBAL['session'] = self._session

        url = self._session.get('path_subs', {}).get(url) or url

        if url.lower().startswith('plugin'):
            try:
                url = self._plugin_request(url)
            except Exception as e:
                log.debug('Plugin requsted failed')
                log.exception(e)
                url = None

        return url

    def _plugin_request(self, url):
        data_path = xbmc.translatePath('special://temp/proxy.post')
        with open(data_path, 'wb') as f:
            f.write(self._post_data or b'')

        url = add_url_args(url, _data_path=data_path, _headers=json.dumps(self._headers))

        log.debug('PLUGIN REQUEST: {}'.format(url))
        dirs, files = xbmcvfs.listdir(url)

        path = unquote(files[0])
        split = path.split('|')
        url = split[0]

        if len(split) > 1:
            self._plugin_headers = dict(parse_qsl(u'{}'.format(split[1]), keep_blank_values=True))

        return url

    def _manifest_middleware(self, data):
        url = self._session.get('manifest_middleware')
        if not url:
            return data

        data_path = xbmc.translatePath('special://temp/proxy.manifest')
        with open(data_path, 'wb') as f:
            f.write(data.encode('utf8'))

        url = add_url_args(url, _data_path=data_path, _headers=json.dumps(self._headers))

        log.debug('PLUGIN MANIFEST MIDDLEWARE REQUEST: {}'.format(url))
        dirs, files = xbmcvfs.listdir(url)

        path = unquote(files[0])
        split = path.split('|')
        data_path = split[0]

        if len(split) > 1:
            self._plugin_headers = dict(parse_qsl(u'{}'.format(split[1]), keep_blank_values=True))

        with open(data_path, 'rb') as f:
            data = f.read().decode('utf8')

        if not ADDON_DEV:
            remove_file(data_path)

        return data

    def do_GET(self):
        url = self._get_url()
        log.debug('GET IN: {}'.format(url))

        if not url:
            response = Response()
            response.headers = {}
            response.status_code = 404
            response.stream = ResponseStream(response)
            response.stream.content = b''
            self._output_response(response)
            return

        response = self._proxy_request('GET', url)

        if self._session.get('redirecting') or not self._session.get('type') or not self._session.get('manifest'):
            self._output_response(response)
            return

        parse = urlparse(self.path.lower())

        try:
            if self._session.get('type') == 'm3u8' and (url == self._session['manifest'] or parse.path.endswith('.m3u') or parse.path.endswith('.m3u8')):
                self._parse_m3u8(response)

            elif self._session.get('type') == 'mpd' and url == self._session['manifest']:
                self._parse_dash(response)
                self._session['manifest'] = None  # unset manifest url so isn't parsed again
        except Exception as e:
            if type(e) != Exit and url == self._session['manifest']:
                gui.error(_.QUALITY_PARSE_ERROR)

            response.status_code = 500
            response.stream.content = str(e).encode('utf-8')

        self._output_response(response)

    def _quality_select(self, qualities):
        def codec_rank(_codecs):
            highest = -1

            for codec in _codecs:
                for key in CODECS:
                    if codec.lower().startswith(key.lower()) and CODECS[key] in CODEC_RANKING:
                        rank = CODEC_RANKING.index(CODECS[key])
                        if not highest or rank > highest:
                            highest = rank

            return highest

        def compare(a, b):
            if a['resolution'] and b['resolution']:
                if int(a['resolution'].split('x')[0]) > int(b['resolution'].split('x')[0]):
                    return 1
                elif int(a['resolution'].split('x')[0]) < int(b['resolution'].split('x')[0]):
                    return -1

            # Same resolution - compare codecs
            a_rank = codec_rank(a['codecs'])
            b_rank = codec_rank(b['codecs'])

            if a_rank > b_rank:
                return 1
            elif a_rank < b_rank:
                return -1

            # Same codec rank - compare bandwidth
            if a['bandwidth'] and b['bandwidth']:
                if a['bandwidth'] > b['bandwidth']:
                    return 1
                elif a['bandwidth'] < b['bandwidth']:
                    return -1

            # Same bandwidth - they are equal (could compare framerate)
            return 0

        def _stream_label(stream):
            try: fps = _(_.QUALITY_FPS, fps=float(stream['frame_rate']))
            except: fps = ''

            codec_string = ''
            for codec in stream['codecs']:
                for key in CODECS:
                    if codec.lower().startswith(key.lower()):
                        codec_string += ' ' + CODECS[key]

            return _(_.QUALITY_BITRATE, bandwidth=int((stream['bandwidth']/10000.0))/100.00, resolution=stream['resolution'], fps=fps, codecs=codec_string.strip()).replace('  ', ' ')

        if self._session.get('selected_quality') is not None:
            if self._session['selected_quality'] in (QUALITY_DISABLED, QUALITY_SKIP):
                return None
            else:
                return qualities[self._session['selected_quality']]

        quality_compare = cmp_to_key(compare)

        quality = int(self._session.get('quality', QUALITY_ASK))
        streams = sorted(qualities, key=quality_compare, reverse=True)

        if not streams:
            quality = QUALITY_DISABLED
        elif len(streams) < 2:
            quality = QUALITY_BEST

        if quality == QUALITY_ASK:
            options = []
            options.append([QUALITY_BEST, _.QUALITY_BEST])

            for x in streams:
                options.append([x, _stream_label(x)])

            options.append([QUALITY_LOWEST, _.QUALITY_LOWEST])
            options.append([QUALITY_SKIP, _.QUALITY_SKIP])

            values = [x[0] for x in options]
            labels = [x[1] for x in options]

            default = -1
            if PROXY_GLOBAL['last_quality'] in values:
                default = values.index(PROXY_GLOBAL['last_quality'])
            else:
                options = [streams[-1]]
                for stream in streams:
                    if PROXY_GLOBAL['last_quality'] >= stream['bandwidth']:
                        options.append(stream)

                default = values.index(sorted(options, key=quality_compare, reverse=True)[0])

            index = gui.select(_.PLAYBACK_QUALITY, labels, preselect=default, autoclose=5000)
            if index < 0:
                raise Exit('Cancelled quality select')

            quality = values[index]
            if index != default:
                PROXY_GLOBAL['last_quality'] = quality['bandwidth'] if quality in qualities else quality
                set_kodi_string('_slyguy_last_quality', PROXY_GLOBAL['last_quality'])

        if quality in (QUALITY_DISABLED, QUALITY_SKIP):
            quality = quality
        elif quality == QUALITY_BEST:
            quality = streams[0]
        elif quality == QUALITY_LOWEST:
            quality = streams[-1]
        elif quality not in streams:
            options = [streams[-1]]
            for stream in streams:
                if quality >= stream['bandwidth']:
                    options.append(stream)
            quality = sorted(options, key=quality_compare, reverse=True)[0]

        if quality in qualities:
            self._session['selected_quality'] = qualities.index(quality)
            return qualities[self._session['selected_quality']]
        else:
            self._session['selected_quality'] = quality
            return None

    def _parse_dash(self, response):
        if ADDON_DEV:
            root = parseString(response.stream.content)
            mpd = root.toprettyxml(encoding='utf-8')
            mpd = b"\n".join([ll.rstrip() for ll in mpd.splitlines() if ll.strip()])
            with open(xbmc.translatePath('special://temp/in.mpd'), 'wb') as f:
                f.write(mpd)

            start = time.time()

        data = response.stream.content.decode('utf8')
        data = self._manifest_middleware(data)

        ## SUPPORT NEW DOLBY FORMAT https://github.com/xbmc/inputstream.adaptive/pull/466
        data = data.replace('tag:dolby.com,2014:dash:audio_channel_configuration:2011', 'urn:dolby:dash:audio_channel_configuration:2011')
        ## SUPPORT EC-3 CHANNEL COUNT https://github.com/xbmc/inputstream.adaptive/pull/618
        data = data.replace('urn:mpeg:mpegB:cicp:ChannelConfiguration', 'urn:mpeg:dash:23003:3:audio_channel_configuration:2011')

        try:
            root = parseString(data.encode('utf8'))
        except Exception as e:
            log.debug("Proxy: Failed to parse MPD")
            log.exception(e)
            return self._output_response(response)

        mpd = root.getElementsByTagName("MPD")[0]

        ## Remove publishTime PR: https://github.com/xbmc/inputstream.adaptive/pull/564
        if 'publishTime' in mpd.attributes.keys():
            mpd.removeAttribute('publishTime')
            log.debug('Dash Fix: publishTime removed')

        ## Live mpd needs non-last periods removed
        ## https://github.com/xbmc/inputstream.adaptive/issues/574
        if 'type' in mpd.attributes.keys() and mpd.getAttribute('type').lower() == 'dynamic':
            periods = [elem for elem in root.getElementsByTagName('Period')]

            # Keep last period
            if len(periods) > 1:
                periods.pop()
                for elem in periods:
                    elem.parentNode.removeChild(elem)
        #################################################

        ## SORT ADAPTION SETS BY BITRATE ##
        video_sets = []
        audio_sets = []
        lang_adap_sets = []
        streams, all_streams, ids = [], [], []
        adap_parent = None

        default_language = self._session.get('default_language', '')
        for adap_set in root.getElementsByTagName('AdaptationSet'):
            adap_parent = adap_set.parentNode

            highest_bandwidth = 0
            is_video = False
            is_trick = False

            for stream in adap_set.getElementsByTagName("Representation"):
                attribs = {}

                ## Make sure Representation are last in adaptionset
                adap_set.removeChild(stream)
                adap_set.appendChild(stream)
                #######

                for key in adap_set.attributes.keys():
                    attribs[key] = adap_set.getAttribute(key)

                for key in stream.attributes.keys():
                    attribs[key] = stream.getAttribute(key)

                if default_language and 'audio' in attribs.get('mimeType', '') and attribs.get('lang').lower() == default_language.lower() and adap_set not in lang_adap_sets:
                    lang_adap_sets.append(adap_set)

                bandwidth = 0
                if 'bandwidth' in attribs:
                    bandwidth = int(attribs['bandwidth'])
                    if bandwidth > highest_bandwidth:
                        highest_bandwidth = bandwidth

                if 'maxPlayoutRate' in attribs:
                    is_trick = True

                if 'video' in attribs.get('mimeType', '') and not is_trick:
                    is_video = True

                    resolution = ''
                    if 'width' in attribs and 'height' in attribs:
                        resolution = '{}x{}'.format(attribs['width'], attribs['height'])

                    frame_rate = ''
                    if 'frameRate'in attribs:
                        frame_rate = attribs['frameRate']
                        try:
                            if '/' in str(frame_rate):
                                split = frame_rate.split('/')
                                frame_rate = float(split[0]) / float(split[1])
                        except:
                            frame_rate = ''

                    codecs = [x for x in attribs.get('codecs', '').split(',') if x]
                    stream  = {'bandwidth': bandwidth, 'resolution': resolution, 'frame_rate': frame_rate, 'codecs': codecs, 'id': attribs['id'], 'elem': stream}
                    all_streams.append(stream)

                    if stream['id'] not in ids:
                        streams.append(stream)
                        ids.append(stream['id'])

            adap_parent.removeChild(adap_set)

            if is_trick:
                continue

            if is_video:
                video_sets.append([highest_bandwidth, adap_set, adap_parent])
            else:
                audio_sets.append([highest_bandwidth, adap_set, adap_parent])

        video_sets.sort(key=lambda  x: x[0], reverse=True)
        audio_sets.sort(key=lambda  x: x[0], reverse=True)

        for elem in video_sets:
            elem[2].appendChild(elem[1])

        for elem in audio_sets:
            elem[2].appendChild(elem[1])

        ## Set default languae
        if lang_adap_sets:
            for elem in root.getElementsByTagName('Role'):
                if elem.getAttribute('schemeIdUri') == 'urn:mpeg:dash:role:2011':
                    elem.parentNode.removeChild(elem)

            for adap_set in lang_adap_sets:
                elem = root.createElement('Role')
                elem.setAttribute('schemeIdUri', 'urn:mpeg:dash:role:2011')
                elem.setAttribute('value', 'main')
                adap_set.appendChild(elem)
                log.debug('default language set to: {}'.format(default_language))
        #############

        ## Insert subtitles
        if adap_parent:
            for idx, subtitle in enumerate(self._session.get('subtitles') or []):
                elem = root.createElement('AdaptationSet')
                elem.setAttribute('mimeType', subtitle[0])
                elem.setAttribute('lang', subtitle[1])
                elem.setAttribute('id', 'caption_{}'.format(idx))

                elem2 = root.createElement('Representation')
                elem2.setAttribute('id', 'caption_rep_{}'.format(idx))

                if 'ttml' in subtitle[0]:
                    elem2.setAttribute('codecs', 'ttml')

                elem3 = root.createElement('BaseURL')
                elem4 = root.createTextNode(subtitle[2])

                elem3.appendChild(elem4)
                elem2.appendChild(elem3)
                elem.appendChild(elem2)

                adap_parent.appendChild(elem)
        ##################

        ## Convert BaseURLS
        base_url_parents = []
        for elem in root.getElementsByTagName('BaseURL'):
            url = elem.firstChild.nodeValue

            if elem.parentNode in base_url_parents:
                log.debug('Non-1st BaseURL removed: {}'.format(url))
                elem.parentNode.removeChild(elem)
                continue

            if '://' not in url:
                url = urljoin(response.url, url)

            elem.firstChild.nodeValue = PROXY_PATH + url
            base_url_parents.append(elem.parentNode)
        ################

        ## Convert to proxy paths
        elems = root.getElementsByTagName('SegmentTemplate')
        elems.extend(root.getElementsByTagName('SegmentURL'))

        def get_base_url(node):
            if not node.parentNode:
                return None

            siblings = node.parentNode.getElementsByTagName('BaseURL')
            if siblings:
                return siblings[0]
            else:
                return get_base_url(node.parentNode)

        for e in elems:
            def process_attrib(attrib):
                if attrib not in e.attributes.keys():
                    return

                url = e.getAttribute(attrib)
                if '://' in url:
                    e.setAttribute(attrib, PROXY_PATH + url)
                else:
                    base_url = get_base_url(e)
                    ## Fixed with https://github.com/xbmc/inputstream.adaptive/pull/606
                    if base_url and not base_url.firstChild.nodeValue.endswith('/'):
                        base_url.firstChild.nodeValue = base_url.firstChild.nodeValue + '/'
                        log.debug('Dash Fix: base_url / fixed')

            process_attrib('initialization')
            process_attrib('media')

            ## Remove presentationTimeOffset PR: https://github.com/xbmc/inputstream.adaptive/pull/564/
            if 'presentationTimeOffset' in e.attributes.keys():
                e.removeAttribute('presentationTimeOffset')
                log.debug('Dash Fix: presentationTimeOffset removed')
        ###############

        ## Get selected quality
        selected = self._quality_select(streams)
        if selected:
            for stream in all_streams:
                if stream['id'] != selected['id']:
                    stream['elem'].parentNode.removeChild(stream['elem'])
        #################

        ## Remove empty adaption sets
        for adap_set in root.getElementsByTagName('AdaptationSet'):
            if not adap_set.getElementsByTagName('Representation'):
                adap_set.parentNode.removeChild(adap_set)
        #################

        if ADDON_DEV:
            mpd = root.toprettyxml(encoding='utf-8')
            mpd = b"\n".join([ll.rstrip() for ll in mpd.splitlines() if ll.strip()])

            log.debug('Time taken: {}'.format(time.time() - start))
            with open(xbmc.translatePath('special://temp/out.mpd'), 'wb') as f:
                f.write(mpd)
        else:
            mpd = root.toxml(encoding='utf-8')

        response.stream.content = mpd

    def _parse_m3u8_master(self, m3u8, master_url):
        def _process_media(line):
            attribs = {}

            for key, value in re.findall('([\w-]+)="?([^",]*)[",$]?', line):
                attribs[key.upper()] = value.strip()

            return attribs

        audio_whitelist   = [x.strip().lower() for x in self._session.get('audio_whitelist', '').split(',') if x]
        subs_whitelist    = [x.strip().lower() for x in self._session.get('subs_whitelist', '').split(',') if x]
        subs_forced       = int(self._session.get('subs_forced', 1))
        subs_non_forced   = int(self._session.get('subs_non_forced', 1))
        audio_description = int(self._session.get('audio_description', 1))
        original_language = self._session.get('original_language', '').lower().strip()
        default_language  = self._session.get('default_language', '').lower().strip()

        if original_language and not default_language:
            default_language = original_language

        if audio_whitelist:
            audio_whitelist.append(original_language)
            audio_whitelist.append(default_language)

        def _lang_allowed(lang, lang_list):
            for _lang in lang_list:
                if not _lang:
                    continue

                if lang.startswith(_lang):
                    return True

            return False

        default_groups = []
        groups = defaultdict(list)
        for line in m3u8.splitlines():
            if not line.startswith('#EXT-X-MEDIA'):
                continue

            attribs = _process_media(line)
            if not attribs:
                continue

            if audio_whitelist and attribs.get('TYPE') == 'AUDIO' and 'LANGUAGE' in attribs and not _lang_allowed(attribs['LANGUAGE'].lower().strip(), audio_whitelist):
                m3u8 = m3u8.replace(line, '')
                continue

            if subs_whitelist and attribs.get('TYPE') == 'SUBTITLES' and 'LANGUAGE' in attribs and not _lang_allowed(attribs['LANGUAGE'].lower().strip(), subs_whitelist):
                m3u8 = m3u8.replace(line, '')
                continue

            if not subs_forced and attribs.get('TYPE') == 'SUBTITLES' and attribs.get('FORCED','').upper() == 'YES':
                m3u8 = m3u8.replace(line, '')
                continue

            if not subs_non_forced and attribs.get('TYPE') == 'SUBTITLES' and attribs.get('FORCED','').upper() != 'YES':
                m3u8 = m3u8.replace(line, '')
                continue

            if not audio_description and attribs.get('TYPE') == 'AUDIO' and attribs.get('CHARACTERISTICS','').lower() == 'public.accessibility.describes-video':
                m3u8 = m3u8.replace(line, '')
                continue

            groups[attribs['GROUP-ID']].append([attribs, line])
            if attribs.get('DEFAULT') == 'YES' and attribs['GROUP-ID'] not in default_groups:
                default_groups.append(attribs['GROUP-ID'])

        if default_language:
            for group_id in groups:
                if group_id in default_groups:
                    continue

                languages = []
                for group in groups[group_id]:
                    attribs, line = group

                    attribs['AUTOSELECT'] = 'NO'
                    attribs['DEFAULT']    = 'NO'

                    if attribs['LANGUAGE'] not in languages or attribs.get('TYPE') == 'SUBTITLES':
                        attribs['AUTOSELECT'] = 'YES'

                        if attribs['LANGUAGE'].lower().strip().startswith(default_language):
                            attribs['DEFAULT'] = 'YES'

                        languages.append(attribs['LANGUAGE'])

        for group_id in groups:
            for group in groups[group_id]:
                attribs, line = group

                # FIX es-ES > es / fr-FR > fr languages #
                if 'LANGUAGE' in attribs:
                    split = attribs['LANGUAGE'].split('-')
                    if len(split) > 1 and split[1].lower() == split[0].lower():
                        attribs['LANGUAGE'] = split[0]
                #############################

                new_line = '#EXT-X-MEDIA:' if attribs else ''
                for key in attribs:
                    new_line += u'{}="{}",'.format(key, attribs[key])

                m3u8 = m3u8.replace(line, new_line.rstrip(','))

        lines = list(m3u8.splitlines())
        line1 = None
        streams, all_streams, urls, metas = [], [], [], []
        for index, line in enumerate(lines):
            if not line.strip():
                continue

            if line.startswith('#EXT-X-STREAM-INF'):
                line1 = index
            elif line1 and not line.startswith('#'):
                attribs = _process_media(lines[line1])

                codecs     = [x for x in attribs.get('CODECS', '').split(',') if x]
                bandwidth  = int(attribs.get('BANDWIDTH') or 0)
                resolution = attribs.get('RESOLUTION', '')
                frame_rate = attribs.get('FRAME_RATE', '')

                url = line
                if '://' in url:
                    url = '/'+'/'.join(url.lower().split('://')[1].split('/')[1:])

                stream = {'bandwidth': int(bandwidth), 'resolution': resolution, 'frame_rate': frame_rate, 'codecs': codecs, 'url': url, 'lines': [line1, index]}
                all_streams.append(stream)

                if stream['url'] not in urls and lines[line1] not in metas:
                    streams.append(stream)
                    urls.append(stream['url'])
                    metas.append(lines[line1])

                line1 = None

        selected = self._quality_select(streams)
        if selected:
            adjust = 0
            for stream in all_streams:
                if stream['url'] != selected['url']:
                    for index in stream['lines']:
                        lines.pop(index-adjust)
                        adjust += 1

        return '\n'.join(lines)

    def _parse_m3u8(self, response):
        m3u8 = response.stream.content.decode('utf8')

        is_master = False
        if '#EXTM3U' not in m3u8:
            raise Exception('Invalid m3u8')

        if '#EXT-X-STREAM-INF' in m3u8:
            is_master = True
            file_name = 'master'
        else:
            file_name = 'sub'

        if ADDON_DEV:
            start = time.time()
            _m3u8 = m3u8.encode('utf8')
            _m3u8 = b"\n".join([ll.rstrip() for ll in _m3u8.splitlines() if ll.strip()])
            with open(xbmc.translatePath('special://temp/'+file_name+'-in.m3u8'), 'wb') as f:
                f.write(_m3u8)

        if is_master:
            m3u8 = self._manifest_middleware(m3u8)
            m3u8 = self._parse_m3u8_master(m3u8, response.url)

        base_url = urljoin(response.url, '/')

        m3u8 = re.sub(r'^/', r'{}'.format(base_url), m3u8, flags=re.I|re.M)
        m3u8 = re.sub(r'URI="/', r'URI="{}'.format(base_url), m3u8, flags=re.I|re.M)

        ## Convert to proxy paths
        m3u8 = re.sub(r'(https?)://', r'{}\1://'.format(PROXY_PATH), m3u8, flags=re.I)

        m3u8 = m3u8.encode('utf8')

        if ADDON_DEV:
            m3u8 = b"\n".join([ll.rstrip() for ll in m3u8.splitlines() if ll.strip()])
            log.debug('Time taken: {}'.format(time.time() - start))
            with open(xbmc.translatePath('special://temp/'+file_name+'-out.m3u8'), 'wb') as f:
                f.write(m3u8)

        response.stream.content = m3u8

    def _proxy_request(self, method, url):
        self._session['redirecting'] = False

        if not url.startswith('http'):
            response = Response()
            response.headers = {}
            response.status_code = 200
            response.stream = ResponseStream(response)

            with open(url, 'rb') as f:
                response.stream.content = f.read()

            if not ADDON_DEV:
                remove_file(url)

            return response

        debug = self._session.get('debug_all') or self._session.get('debug_{}'.format(method.lower()))
        if self._post_data and debug:
            with open(xbmc.translatePath('special://temp/{}-request.txt').format(method.lower()), 'wb') as f:
                f.write(self._post_data)

        if not self._session.get('session'):
            self._session['session'] = RawSession()
        else:
            self._session['session'].headers.clear()
            #self._session['session'].cookies.clear() #lets handle cookies in session

        ## Fix any double // in url
        url = fix_url(url)

        retries = 3
        # some reason we get connection errors every so often when using a session. something to do with the socket
        for i in range(retries):
            try:
                response = self._session['session'].request(method=method, url=url, headers=self._headers, data=self._post_data, allow_redirects=False, stream=True)
            except ConnectionError as e:
                if 'Connection aborted' not in str(e) or i == retries-1:
                    log.exception(e)
                    raise
            except Exception as e:
                log.exception(e)
                raise
            else:
                break

        response.stream = ResponseStream(response)

        log.debug('{} OUT: {} ({})'.format(method.upper(), url, response.status_code))

        headers = {}
        for header in response.headers:
            if header.lower() not in REMOVE_OUT_HEADERS:
                headers[header.lower()] = response.headers[header]

        response.headers = headers

        if debug:
            with open(xbmc.translatePath('special://temp/{}-response.txt').format(method.lower()), 'wb') as f:
                f.write(response.stream.content)

        if 'location' in response.headers:
            if '://' not in response.headers['location']:
                response.headers['location'] = urljoin(url, response.headers['location'])

            self._session['redirecting'] = True
            if url == self._session.get('manifest'):
                self._session['manifest'] = response.headers['location']

            response.headers['location'] = PROXY_PATH + response.headers['location']
            response.stream.content = b''

        if 'set-cookie' in response.headers:
            log.debug('set-cookie: {}'.format(response.headers['set-cookie']))
            response.headers.pop('set-cookie') #lets handle cookies in session
            # base_url = urljoin(url, '/')
            # response.headers['set-cookie'] = re.sub(r'domain=([^ ;]*)', r'domain={}'.format(PROXY_HOST), response.headers['set-cookie'], flags=re.I)
            # response.headers['set-cookie'] = re.sub(r'path=(/[^ ;]*)', r'path=/{}\1'.format(base_url), response.headers['set-cookie'], flags=re.I)

        return response

    def _output_headers(self, response):
        self.send_response(response.status_code)

        response.headers.update(self._plugin_headers)

        if 'content-length' in response.headers:
            response.headers['keep-alive'] = 'timeout=10, max=1000'
        else:
            response.headers['connection'] = 'close'

        for d in list(response.headers.items()):
            self.send_header(d[0], d[1])

        self.end_headers()

    def _output_response(self, response):
        self._output_headers(response)

        for chunk in response.stream.iter_content():
            try:
                self.wfile.write(chunk)
            except Exception as e:
                break

    def do_HEAD(self):
        url = self._get_url()
        log.debug('HEAD IN: {}'.format(url))
        response = self._proxy_request('HEAD', url)
        self._output_response(response)

    def do_POST(self):
        url = self._get_url()
        log.debug('POST IN: {}'.format(url))
        response = self._proxy_request('POST', url)
        self._output_response(response)

class Response(object):
    pass

class ResponseStream(object):
    def __init__(self, response):
        self._response = response
        self._bytes = None

    @property
    def content(self):
        if not self._bytes:
            self.content = self._response.content

        return self._bytes

    @content.setter
    def content(self, _bytes):
        if not type(_bytes) is bytes:
            raise Exception('Only bytes allowed when setting content')

        self._bytes = _bytes
        self._response.headers['content-length'] = str(len(_bytes))
        self._response.headers.pop('content-range', None)
        self._response.headers.pop('content-encoding', None)

    def iter_content(self):
        if self._bytes is not None:
            yield self._bytes
        else:
            while True:
                chunk = self._response.raw.read(CHUNK_SIZE)
                if not chunk:
                    break

                yield chunk

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

class Proxy(object):
    def start(self):
        log.debug("Starting Proxy {}:{}".format(PROXY_HOST, PROXY_PORT))
        self._server = ThreadedHTTPServer((PROXY_HOST, PROXY_PORT), RequestHandler)
        self._server.allow_reuse_address = True
        self._httpd_thread = threading.Thread(target=self._server.serve_forever)
        self._httpd_thread.start()
        log.info("Proxy bound to {}:{}".format(PROXY_HOST, PROXY_PORT))

    def stop(self):
        self._server.shutdown()
        self._server.server_close()
        self._server.socket.close()
        self._httpd_thread.join()
        log.debug("Proxy Server: Stopped")
