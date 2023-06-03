import locale
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import toml
from uptime_kuma_api import UptimeKumaApi, MonitorType


def add_monitor(api: UptimeKumaApi, name: str, tag_id: int) -> dict:
    new = api.add_monitor(
        type=MonitorType.PUSH,
        name=name.title(),
        interval=70,
    )
    api.add_monitor_tag(
        tag_id=tag_id,
        monitor_id=new['monitorID']
    )
    new = api.get_monitor(new['monitorID'])

    return {
        'id': new['id'],
        'name': new['name'],
        'pushToken': new['pushToken'],
    }


def fetch_tags(api: UptimeKumaApi) -> dict:
    output: dict = {
        tag["name"]: tag["id"]
        for tag in api.get_tags()
    }

    return output


def delete_monitor(api: UptimeKumaApi, mid: int) -> None:
    api.delete_monitor(mid)


def ping_monitors(uptime_data: dict, backend_data: dict):
    urls = []

    # prepare urls
    for area, status in backend_data.items():
        url_token = uptime_data[area]['pushToken']
        msg = 'up' if status else 'down'
        area_url = f"{config['uptime']['url']}/api/push/{url_token}?status={msg}"
        urls.append(area_url)

    print(f"Pinging {len(urls)} URLs")

    with ThreadPoolExecutor(max_workers=config['general']['max_workers']) as executor:
        futures = []

        for url in urls:
            futures.append(executor.submit(ping_status, url))

        for future in as_completed(futures):
            try:
                res = future.result()
            except Exception as ex:
                print(f"Ping of URL {res.url} failed with {ex}")


def ping_status(url: str) -> requests.Response:
    return requests.get(url, timeout=config['general']['timeout'])


def polish_sort_key(word: str) -> list[int]:
    polish_alphabet = 'AĄBCĆDEĘFGHIJKLŁMNŃOÓPRSŚTUWYZŹŻaąbcćdeęfghijklłmnńoóprsśtuwyzźż'
    return [polish_alphabet.find(c) if c in polish_alphabet else ord(c) for c in word]


if __name__ == '__main__':
    config_path = os.path.join(os.getcwd(), 'config.toml')
    with open(config_path, 'r') as f:
        config = toml.load(f)

    locale.setlocale(locale.LC_ALL, config['general']['locale'])

    uptime_monitors = {}
    backend_areas = {}

    while True:
        # get a list of workers from backend
        try:
            backend_status = requests.get(config['backend']['url'], timeout=config['general']['timeout']).json()
        except Exception as e:
            print(e)
            time.sleep(config['general']['error_sleep'])
            continue

        # maybe rework a logic? someday.
        backend_areas = {
            area['name']: (
                area['worker_managers'][0]['active_workers'] / area['worker_managers'][0]['expected_workers']
                >= config['backend']['threshold']
            ) for area in backend_status['areas'] if area['worker_managers'][0]['expected_workers'] != 0
        }

        # check for changes
        if sorted(uptime_monitors.keys()) != sorted(backend_areas.keys()) or len(backend_areas) == 0:
            print("Checking Kuma API")

            try:
                # login to uptime-kuma
                kuma = UptimeKumaApi(config['uptime']['url'])
                kuma.login(config['uptime']['login'], config['uptime']['password'])
                kuma_tag = fetch_tags(kuma)[config['uptime']['tag_name']]

                # get a list of existing workers on uptime-kuma
                monitors = kuma.get_monitors()
                for monitor in monitors:
                    if any(tag['tag_id'] == kuma_tag for tag in monitor['tags']):
                        uptime_monitors[monitor['name']] = {
                            'id': monitor['id'],
                            'name': monitor['name'],
                            'pushToken': monitor['pushToken'],
                        }

                # check missing areas on uptime-kuma side and add them
                for area in backend_areas.keys():
                    if area not in uptime_monitors.keys():
                        print(f"Added new monitor {area}")
                        new_monitor = add_monitor(kuma, area, kuma_tag)
                        uptime_monitors[new_monitor['name']] = new_monitor

                # check old entries on uptime-kuma and remove them
                to_remove = []
                for area, data in uptime_monitors.items():
                    if area not in backend_areas.keys():
                        print(f"Removed old monitor {area} with id:{data['id']}")
                        delete_monitor(kuma, data['id'])
                        to_remove.append(area)

                # drop old entries
                for area in to_remove:
                    del(uptime_monitors[area])

                # update status page
                status_page = kuma.get_status_page(config['uptime']['slug'])

                # drop fields before saving
                del(status_page["incident"])
                del(status_page["maintenanceList"])

                # find
                for row in status_page["publicGroupList"]:
                    if row["name"] == config['uptime']['group']:
                        # overwrite desired group
                        row['monitorList'] = sorted([{
                                'id': m['id'],
                                'name': m['name'],
                                'sendUrl': 0
                            } for m in uptime_monitors.values()
                        ], key=lambda m: polish_sort_key(m['name']))

                # save page!
                kuma.save_status_page(**status_page)

                # close uptime-kuma connection
                kuma.disconnect()
            except Exception as e:
                print(f"Kuma ded? {e}")

        # ping devices
        ping_monitors(uptime_monitors, backend_areas)

        # do something with the workers list
        time.sleep(config['general']['sleep'])
