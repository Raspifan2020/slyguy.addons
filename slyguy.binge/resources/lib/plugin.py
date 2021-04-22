import codecs
import random
import time
import math
import re

import arrow
from kodi_six import xbmc, xbmcplugin

from slyguy import plugin, gui, settings, userdata, signals, inputstream
from slyguy.log import log
from slyguy.session import Session
from slyguy.exceptions import PluginError
from slyguy.constants import ROUTE_LIVE_SUFFIX, ROUTE_LIVE_TAG, PLAY_FROM_TYPES, PLAY_FROM_ASK, PLAY_FROM_LIVE, PLAY_FROM_START

from .api import API, APIError
from .language import _
from .constants import *

api = API()

@signals.on(signals.BEFORE_DISPATCH)
def before_dispatch():
    api.new_session()
    plugin.logged_in = api.logged_in

@plugin.route('')
def home(**kwargs):
    folder = plugin.Folder(cacheToDisc=False)

    if not api.logged_in:
        folder.add_item(label=_(_.LOGIN, _bold=True),  path=plugin.url_for(login), bookmark=False)
    else:
        folder.add_item(label=_(_.FEATURED, _bold=True), path=plugin.url_for(landing, slug='home', title=_.FEATURED))
        folder.add_item(label=_(_.SHOWS, _bold=True), path=plugin.url_for(landing, slug='shows', title=_.SHOWS))
        folder.add_item(label=_(_.MOVIES, _bold=True), path=plugin.url_for(landing, slug='movies', title=_.MOVIES))
       # folder.add_item(label=_(_.BINGE_LISTS, _bold=True), path=plugin.url_for(landing, slug='watchlist', title=_.BINGE_LISTS))
        folder.add_item(label=_(_.LIVE_CHANNELS, _bold=True), path=plugin.url_for(panel, panel_id=CHANNELS_PANEL, title=_.LIVE_CHANNELS))
        folder.add_item(label=_(_.SEARCH, _bold=True), path=plugin.url_for(search))

        if settings.getBool('bookmarks', True):
            folder.add_item(label=_(_.BOOKMARKS, _bold=True),  path=plugin.url_for(plugin.ROUTE_BOOKMARKS), bookmark=False)

        folder.add_item(label=_.SELECT_PROFILE, path=plugin.url_for(select_profile), art={'thumb': _get_avatar(userdata.get('avatar_id'))}, info={'plot': userdata.get('profile_name')}, _kiosk=False, bookmark=False)
        folder.add_item(label=_.LOGOUT, path=plugin.url_for(logout), _kiosk=False, bookmark=False)

    folder.add_item(label=_.SETTINGS, path=plugin.url_for(plugin.ROUTE_SETTINGS), _kiosk=False, bookmark=False)

    return folder

@plugin.route()
def landing(slug, title, **kwargs):
    folder = plugin.Folder(title)
    folder.add_items(_landing(slug))
    return folder

def _landing(slug, params=None):
    items = []

    to_add = []

    def expand(row):
        if not row['personalised'] and row.get('contents'):
            items.extend(_parse_contents(row.get('contents', [])))
        else:
            data = api.panel(link=row['links']['panels'])
            items.extend(_parse_contents(data.get('contents', [])))

    for row in api.landing(slug, params)['panels']:
        if row['panelType'] == 'hero-carousel' and settings.getBool('show_hero_contents', True):
            expand(row)

        elif row['panelType'] not in ('hero-carousel', 'genre-menu-sticky') and 'id' in row:
            to_add.append(row)

    if not items and len(to_add) == 1:
        expand(to_add[0])
    else:
        for row in to_add:
            items.append(plugin.Item(
                label = row['title'],
                path  = plugin.url_for(panel, link=row['links']['panels']),
            ))

    return items

@plugin.route()
def genre(slug, title, genre, subgenre=None, **kwargs):
    folder = plugin.Folder(title)

    params = {'genre': genre}
    if subgenre:
        params['subgenre'] = subgenre

    folder.add_items(_landing(slug, params))
    return folder

@plugin.route()
def search(query=None,**kwargs):
    if not query:
        query = gui.input(_.SEARCH, default=userdata.get('search', '')).strip()
        if not query:
            return

        userdata.set('search', query)

    data = api.search(query=query)

    folder = plugin.Folder(_(_.SEARCH_FOR, query=query))

    if 'panels' in data:
        items = _parse_contents(data['panels'][0].get('contents', []))
        folder.add_items(items)

    return folder

@plugin.route()
def show(show_id, title, **kwargs):
    folder = plugin.Folder(title)

    data = api.landing('show', {'show': show_id})

    seasons = []
    episodes = []
    heros = []
    for row in data['panels']:
        if row['panelType'] == 'tags':
            for row in row.get('contents', []):
                item = plugin.Item(
                    label = row['data']['clickthrough']['title'],
                    art  = {
                        'thumb' : data['meta']['socialImage'].replace('${WIDTH}', str(768)),
                        'fanart': row['data']['contentDisplay']['images']['hero'].replace('${WIDTH}', str(1920)),
                    },
                    info = {
                        'plot': row['data']['contentDisplay']['synopsis'],
                    },
                    path = plugin.url_for(season, show_id=show_id, season_id=row['data']['id'], title=title),
                )
                seasons.append(item)
        elif row['panelType'] == 'synopsis-carousel-tabbed' and row['title'] == 'Episodes':
            episodes.extend(_parse_contents(row.get('contents', [])))
        elif row['panelType'] == 'hero-carousel':
            heros.extend(_parse_contents(row.get('contents', [])))

    if seasons:
        folder.add_items(seasons)
    elif episodes:
        folder.sort_methods = [xbmcplugin.SORT_METHOD_EPISODE, xbmcplugin.SORT_METHOD_UNSORTED, xbmcplugin.SORT_METHOD_LABEL, xbmcplugin.SORT_METHOD_DATEADDED]
        folder.add_items(episodes)
    else:
        folder.add_items(heros)

    return folder

@plugin.route()
def season(show_id, season_id, title, **kwargs):
    data = api.landing('show', {'show': show_id, 'season': season_id})

    folder = plugin.Folder(title, sort_methods=[xbmcplugin.SORT_METHOD_EPISODE, xbmcplugin.SORT_METHOD_UNSORTED, xbmcplugin.SORT_METHOD_LABEL, xbmcplugin.SORT_METHOD_DATEADDED])

    for row in data['panels']:
        if row['panelType'] == 'synopsis-carousel-tabbed':
            items = _parse_contents(row.get('contents', []))
            folder.add_items(items)

    return folder

@plugin.route()
def panel(panel_id=None, link=None, title=None, **kwargs):
    data = api.panel(panel_id=panel_id, link=link)

    folder = plugin.Folder(title or data['title'])
    folder.add_items(_parse_contents(data.get('contents', [])))

    return folder

def _makeTime(start=None):
    return start.to('local').format('h:mmA') if start else ''

def _makeDate(now, start=None):
    if not start:
        return ''

    if now.year == start.year:
        return start.to('local').format('DD MMM')
    else:
        return start.to('local').format('DD MMM YY')

def _makeHumanised(now, start=None):
    if not start:
        return ''

    now   = now.to('local').replace(hour = 0, minute = 0, second = 0, microsecond = 0)
    start = start.to('local').replace(hour = 0, minute = 0, second = 0, microsecond = 0)
    days  = (start - now).days

    if days == -1:
        return 'yesterday'
    elif days == 0:
        return 'today'
    elif days == 1:
        return 'tomorrow'
    elif days <= 7 and days >= 1:
        return start.format('dddd')
    else:
        return _makeDate(now, start)

def _get_asset(row):
    asset = {
        'id': row['id'],
        'type': row.get('type'),

        'plot': row['contentDisplay']['synopsis'],
        'title': row['contentDisplay']['title']['value'],
        'thumb': row['contentDisplay']['images']['tile'].replace('${WIDTH}', str(768)),
        'fanart': row['contentDisplay']['images']['hero'].replace('${WIDTH}', str(1920)),

        'transmissionTime': row['clickthrough']['transmissionTime'],
        'preCheckTime': row['clickthrough'].get('preCheckTime'),
        'isStreaming': row['clickthrough']['isStreaming'],
        'asset_id': row['clickthrough']['asset'],

        'playbackType': None,
    }

    if 'playback' in row:
        asset.update({
            'asset_id': row['playback']['info']['assetId'],
            'playbackType': row['playback']['info']['playbackType'],
            'showtitle': row['playback']['info'].get('show'),
            'duration': row['playback']['info'].get('mediaDuration'),
        })

    for line in row['contentDisplay']['infoLine']:
        if line['type'] == 'episode':
            season  = re.search('S([0-9]+)', line['value'])
            episode = re.search('EP([0-9]+)', line['value'])
            if episode:
                asset['episode'] = int(episode.group(1))
            if season:
                asset['season'] = int(season.group(1))
        elif line['type'] == 'imdb':
            asset['rating'] = line['value']
        elif line['type'] == 'years':
            asset['year'] = int(line['value'])
        # elif line['type'] == 'length' and not asset.get('duration'):
        #     asset['duration'] = 0
        #     match = re.search('([0-9]+)h', line['value'], re.IGNORECASE)
        #     if match:
        #        asset['duration'] += int(match.group(1))*3600
        #     match = re.search('([0-9]+)m', line['value'], re.IGNORECASE)
        #     if match:
        #        asset['duration'] += int(match.group(1))*60
        #     match = re.search('([0-9]+)s', line['value'], re.IGNORECASE)
        #     if match:
        #        asset['duration'] += int(match.group(1))

    return asset

def _parse_contents(rows):
    items = []

    for row in rows:
        asset = _get_asset(row['data'])

        if row['contentType'] == 'video' and asset['asset_id']:
            items.append(_parse_video(asset))

        elif row['contentType'] == 'section' and row['data']['type'] == 'feature-film':
            items.append(_parse_video(asset))

        elif row['contentType'] == 'section' and row['data']['type'] == 'tv-show':
            items.append(_parse_show(asset))

        elif row['contentType'] == 'section' and row['data']['contentType'] == 'genre-menu':

            items.append(plugin.Item(
                label = row['data']['clickthrough']['title'],
                art  = {
                    'thumb' : row['data']['contentDisplay']['images']['menuItemSelected'].replace('${WIDTH}', str(320)),
                },
                path  = plugin.url_for(genre, slug=row['data']['clickthrough']['type'], title=row['data']['clickthrough']['title'], genre=row['data']['clickthrough']['genre'], subgenre=row['data']['clickthrough']['subgenre']),
            ))

        # elif row['contentType'] == 'section' and row['data']['contentType'] == 'collection':
        #     items.append(_parse_collection(asset))

    return items

def _parse_collection(asset):
    return plugin.Item(
        label = asset['title'],
        art  = {
            'thumb' : asset['thumb'],
            'fanart': asset['fanart'],
        },
        info = {
            'plot': asset['plot'],
        },
        path = plugin.url_for(collection, collection_id=asset['id'], title=asset['title']),
    )

@plugin.route()
def collection():
    pass

def _parse_show(asset):
    return plugin.Item(
        label = asset['title'],
        art  = {
            'thumb' : asset['thumb'],
            'fanart': asset['fanart'],
        },
        info = {
            'plot': asset['plot'],
        },
        path = plugin.url_for(show, show_id=asset['id'], title=asset['title']),
    )

def _parse_video(asset):
    alerts   = userdata.get('alerts', [])
    now      = arrow.now()
    start    = arrow.get(asset['transmissionTime'])
    precheck = start

    if asset['preCheckTime']:
        precheck = arrow.get(asset['preCheckTime'])
        if precheck > start:
            precheck = start

    # if 'heroHeader' in row['contentDisplay']:
    #     title += ' [' + row['contentDisplay']['heroHeader'].replace('${DATE_HUMANISED}', _makeHumanised(now, start).upper()).replace('${TIME}', _makeTime(start)) + ']'

    item = plugin.Item(
        label = asset['title'],
        art  = {
            'thumb' : asset['thumb'],
            'fanart': asset['fanart'],
        },
        info = {
            'plot': asset['plot'],
            'rating': asset.get('rating'),
            'season': asset.get('season'),
            'episode': asset.get('episode'),
            'tvshowtitle': asset.get('showtitle'),
            'duration': asset.get('duration'),
            'year': asset.get('year'),
            'mediatype': 'episode' if asset.get('episode') else 'movie',
        },
        playable = True,
        is_folder = False,
    )

    is_live    = False
    play_type  = settings.getEnum('live_play_type', PLAY_FROM_TYPES, default=PLAY_FROM_ASK)
    start_from = ((start - precheck).seconds)

    if start_from < 0:
        start_from = 0

    if now < start:
        is_live = True
        toggle_alert = plugin.url_for(alert, asset=asset['asset_id'], title=asset['title'])

        if asset['asset_id'] not in userdata.get('alerts', []):
            item.info['playcount'] = 0
            item.context.append((_.SET_REMINDER, "RunPlugin({})".format(toggle_alert)))
        else:
            item.info['playcount'] = 1
            item.context.append((_.REMOVE_REMINDER, "RunPlugin({})".format(toggle_alert)))

    elif asset['type'] == 'live-linear':
        is_live = True
        start_from = 0
        play_type = PLAY_FROM_START

    elif asset['playbackType'] == 'LIVE' and click['isStreaming']:
        is_live = True

        item.context.append((_.PLAY_FROM_LIVE, "PlayMedia({})".format(
            plugin.url_for(play, id=asset['asset_id'], play_type=PLAY_FROM_LIVE, _is_live=is_live)
        )))

        item.context.append((_.PLAY_FROM_START, "PlayMedia({})".format(
            plugin.url_for(play, id=asset['asset_id'], start_from=start_from, play_type=PLAY_FROM_START, _is_live=is_live)
        )))

    item.path = plugin.url_for(play, id=asset['asset_id'], start_from=start_from, play_type=play_type, _is_live=is_live)

    return item

@plugin.route()
def login(**kwargs):
    if gui.yes_no(_.LOGIN_WITH, yeslabel=_.DEVICE_LINK, nolabel=_.EMAIL_PASSWORD):
        result = _device_link()
    else:
        result = _email_password()

    if not result:
        return

    _select_profile()
    gui.refresh()

def _device_link():
    start     = time.time()
    data      = api.device_code()
    monitor   = xbmc.Monitor()

    with gui.progress(_(_.DEVICE_LINK_STEPS, url=data['verification_uri'], code=data['user_code']), heading=_.DEVICE_LINK) as progress:
        while (time.time() - start) < data['expires_in']:
            for i in range(data['interval']):
                if progress.iscanceled() or monitor.waitForAbort(1):
                    return

                progress.update(int(((time.time() - start) / data['expires_in']) * 100))

            if api.device_login(data['device_code']):
                return True

def _email_password():
    username = gui.input(_.ASK_USERNAME, default=userdata.get('username', '')).strip()
    if not username:
        return

    userdata.set('username', username)

    password = gui.input(_.ASK_PASSWORD, hide_input=True).strip()
    if not password:
        return

    api.login(username=username, password=password)

    return True

@plugin.route()
@plugin.login_required()
def logout(**kwargs):
    if not gui.yes_no(_.LOGOUT_YES_NO):
        return

    api.logout()
    userdata.delete('avatar_id')
    userdata.delete('profile_name')
    userdata.delete('profile_id')
    gui.refresh()

@plugin.route()
@plugin.login_required()
def select_profile(**kwargs):
    _select_profile()
    gui.refresh()

def _select_profile():
    profiles = api.profiles()

    options = []
    values  = []
    can_delete = []
    default = -1

    for index, profile in enumerate(profiles):
        values.append(profile)
        options.append(plugin.Item(label=profile['name'], art={'thumb': _get_avatar(profile['avatar_id'])}))

        if profile['id'] == userdata.get('profile_id'):
            default = index
            _set_profile(profile, notify=False)

        elif not profile['root_flag']:
            can_delete.append(profile)

    options.append(plugin.Item(label=_(_.ADD_PROFILE, _bold=True)))
    values.append('_add')

    if can_delete:
        options.append(plugin.Item(label=_(_.DELETE_PROFILE, _bold=True)))
        values.append('_delete')

    index = gui.select(_.SELECT_PROFILE, options=options, preselect=default, useDetails=True)
    if index < 0:
        return

    selected = values[index]

    if selected == '_delete':
        _delete_profile(can_delete)
    elif selected == '_add':
        _add_profile(taken_names=[x['name'].lower() for x in profiles], taken_avatars=[x['avatar_id'] for x in profiles])
    else:
        _set_profile(selected)

def _get_avatar(avatar_id):
    if avatar_id is None:
        return None

    return AVATAR_URL.format(avatar_id=avatar_id)

def _set_profile(profile, notify=True):
    userdata.set('avatar_id', profile['avatar_id'])
    userdata.set('profile_name', profile['name'])
    userdata.set('profile_id', profile['id'])

    if notify:
        gui.notification(_.PROFILE_ACTIVATED, heading=profile['name'], icon=_get_avatar(profile['avatar_id']))

def _delete_profile(profiles):
    options = []
    for index, profile in enumerate(profiles):
        options.append(plugin.Item(label=profile['name'], art={'thumb': _get_avatar(profile['avatar_id'])}))

    index = gui.select(_.SELECT_DELETE_PROFILE, options=options, useDetails=True)
    if index < 0:
        return

    selected = profiles[index]
    if gui.yes_no(_.DELETE_PROFILE_INFO, heading=_(_.DELTE_PROFILE_HEADER, name=selected['name'])) and api.delete_profile(selected).ok:
        gui.notification(_.PROFILE_DELETED, heading=selected['name'], icon=_get_avatar(selected['avatar_id']))

def _add_profile(taken_names, taken_avatars):
    ## PROFILE AVATAR ##
    options = [plugin.Item(label=_(_.RANDOM_AVATAR, _bold=True)),]
    values  = ['_random',]
    avatars = []
    unused  = []

    for avatar in api.profile_config()['avatars']:
        values.append(avatar['id'])
        avatars.append(avatar['id'])

        if avatar['id'] in taken_avatars:
            label = _(_.AVATAR_USED, _bold=True)
        else:
            label =_.AVATAR_NOT_USED
            unused.append(avatar['id'])

        options.append(plugin.Item(label=label, art={'thumb': _get_avatar(avatar['id'])}))

    index = gui.select(_.SELECT_AVATAR, options=options, useDetails=True)
    if index < 0:
        return

    avatar_id = values[index]
    if avatar_id == '_random':
        avatar_id = random.choice(unused or avatars)

    ## PROFILE NAME ##
    name = ''
    while True:
        name = gui.input(_.PROFILE_NAME, default=name).strip()
        if not name:
            return

        elif name.lower() in taken_names:
            gui.notification(_(_.PROFILE_NAME_TAKEN, name=name))

        else:
            break

    ## ADD PROFILE ##
    profile = api.add_profile(name, avatar_id)
    if 'message' in profile:
        raise PluginError(profile['message'])

    _set_profile(profile)

@plugin.route()
@plugin.plugin_callback()
def license_request(_data, _data_path, **kwargs):
    data = api.license_request(_data)

    with open(_data_path, 'wb') as f:
        f.write(data)

    return _data_path

# @plugin.route()
# @plugin.plugin_callback()
# def license_request(**kwargs):
#     api.refresh_token()
#     item = plugin.Item(path=LICENSE_URL, headers=HEADERS)
#     item.headers.update({'authorization': 'Bearer {}'.format(userdata.get('access_token'))})
#     return item.get_li().getPath()

@plugin.route()
@plugin.login_required()
def play(id, start_from=0, play_type=PLAY_FROM_LIVE, **kwargs):
    asset      = api.stream(id)

    start_from = int(start_from)
    play_type  = int(play_type)
    is_live    = kwargs.get(ROUTE_LIVE_TAG) == ROUTE_LIVE_SUFFIX

    streams = [asset['recommendedStream']]
    streams.extend(asset['alternativeStreams'])
    streams = [s for s in streams if s['mediaFormat'] in SUPPORTED_FORMATS]

    if not streams:
        raise PluginError(_.NO_STREAM)

    providers = SUPPORTED_PROVIDERS[:]
    providers.extend([s['provider'] for s in streams])

    streams  = sorted(streams, key=lambda k: (providers.index(k['provider']), SUPPORTED_FORMATS.index(k['mediaFormat'])))
    stream   = streams[0]

    log.debug('Stream CDN: {provider} | Stream Format: {mediaFormat}'.format(**stream))

    item = plugin.Item(
        path     = stream['manifest']['uri'],
        art      = False,
        headers  = HEADERS,
        use_proxy = True, #required to support dolby 5.1 and license requests
    )

    if is_live and (play_type == PLAY_FROM_LIVE or (play_type == PLAY_FROM_ASK and gui.yes_no(_.PLAY_FROM, yeslabel=_.PLAY_FROM_LIVE, nolabel=_.PLAY_FROM_START))):
        play_type = PLAY_FROM_LIVE
        start_from = 0

    if stream['mediaFormat'] == FORMAT_DASH:
        item.inputstream = inputstream.MPD()

    elif stream['mediaFormat'] == FORMAT_HLS_TS:
        force = (is_live and play_type == PLAY_FROM_LIVE)
        item.inputstream = inputstream.HLS(force=force, live=is_live)

        if force and not item.inputstream.check():
            raise PluginError(_.HLS_REQUIRED)

    elif stream['mediaFormat'] == FORMAT_HLS_FMP4:
        item.inputstream = inputstream.HLS(force=True, live=is_live)
        if not item.inputstream.check():
            raise PluginError(_.HLS_REQUIRED)

    elif stream['mediaFormat'] in (FORMAT_DRM_DASH, FORMAT_DRM_DASH_HEVC):
        item.inputstream = inputstream.Widevine(license_key=plugin.url_for(license_request))

    if start_from:
        item.properties['ResumeTime'] = start_from
        item.properties['TotalTime']  = start_from

    return item

@plugin.route()
@plugin.merge()
def playlist(output, **kwargs):
    data  = api.panel(panel_id=CHANNELS_PANEL)

    try: chnos = Session().get(CHNO_URL).json()
    except: chnos = {}

    with codecs.open(output, 'w', encoding='utf8') as f:
        f.write(u'#EXTM3U\n')

        for row in data.get('contents', []):
            if row['data']['type'] != 'live-linear':
                continue

            chid = row['data']['playback']['info']['assetId']
            chno = chnos.get(chid) or ''

            f.write(u'#EXTINF:-1 tvg-id="{id}" tvg-chno="{channel}" channel-id="{channel}" tvg-logo="{logo}",{name}\n{path}\n'.format(
                id=chid, channel=chno, logo=row['data']['contentDisplay']['images']['tile'].replace('${WIDTH}', str(768)),
                    name=row['data']['playback']['info']['title'], path=plugin.url_for(play, id=chid, play_type=PLAY_FROM_START, _is_live=True)))