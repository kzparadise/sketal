from typing import Union

from captcha_solver import CaptchaSolver

from methods import *
from utils import *
from vk_api import *
from vk_plus_utils import *


class VkController(object):
    __slots__ = ("logger", "vk_users", "vk_groups", "scope", "group", "app_id",
                 "current_user", "current_token", "hold", "proxies", "users_data",
                 "solver", "target_client")

    def __init__(self, settings, logger=None):
        if logger:
            self.logger = logger
        else:
            self.logger = logging.Logger("vk_controller")

        self.vk_users = []
        self.vk_groups = []
        self.scope = settings.SCOPE
        self.group = False
        self.app_id = settings.APP_ID
        self.current_user = 0
        self.current_token = 0

        self.target_client = None

        self.hold = 0

        self.proxies = settings.PROXIES
        if not self.proxies:
            self.proxies = []

        self.users_data = settings.USERS
        if not self.users_data:
            self.users_data = []

        self.solver = None
        if settings.CAPTCHA_KEY and settings.CAPTCHA_KEY:
            self.solver = CaptchaSolver(settings.CAPTCHA_SERVER, api_key=settings.CAPTCHA_KEY)

        loop = asyncio.get_event_loop()
        if not loop.is_running():
            loop.run_until_complete(self.init_vk())

    async def init_vk(self):
        """Add possible executor for vk methods"""

        current_proxy = 0

        for i, user in enumerate(self.users_data):
            if self.proxies:
                proxy = self.proxies[current_proxy % len(self.proxies)]
                current_proxy += 1

            else:
                proxy = None

            if user[0] == "group":
                client = VkClient(proxy, logger=self.logger)

                await client.group(user[1])

                if self.target_client is None: self.target_client = Sender(group=True, target=0)

                self.vk_groups.append(client)
                self.group = True

            else:
                client = VkClient(proxy, logger=self.logger)

                if len(user) == 2:
                    await client.user_with_token(user[1])

                else:
                    await client.user(user[1], user[2], self.app_id, self.scope)

                if self.target_client is None: self.target_client = Sender(user=True, target=0)

                self.vk_users.append(client)

    def get_bot_main(self):
        return

    def create_proxy(self, outer_name, sender=None, wait=Wait.YES):
        """Create Proxy for nice looking mthod calls"""

        if outer_name == "execute":
            async def wrapper(**data):
                return await self.vk_controller.method("execute", data, sender, wait)

            return wrapper

        return Proxy(self, outer_name, sender, wait)

    @contextmanager
    def mass_request(self):
        """Contexmanager for cases when user need to perform many requests at once"""

        self.hold += 1

        for client in self.vk_users + self.vk_groups:
            client.queue.hold = True

        yield

        self.hold -= 1

        if self.hold < 1:
            for client in self.vk_users + self.vk_groups:
                client.queue.hold = False

    def __call__(self, sender=None, wait=Wait.YES):
        return ProxyParametrs(self, sender, wait)

    def __getattr__(self, outer_name):
        return self.create_proxy(outer_name)

    async def method(self, key, data=None, sender=None, wait=Wait.YES):
        """ Execute vk method `key` with parameters `data` with sender settings `sender` with waiting settings `wait`
        and return results. False or None if something failed.
        """

        client = self.get_current_sender(key, sender)

        if not client:
            self.logger.error(f"No executor for \"{key}\"!")
            return False

        task = Request(key, data, sender)
        await client.queue.enqueue(task)

        if wait == Wait.NO:
            return None

        elif wait == Wait.YES:
            try:
                return await asyncio.wait_for(task, 90)
            except asyncio.CancelledError:
                pass
            except Exception:
                import traceback
                traceback.print_exc()

        elif wait == Wait.CUSTOM:
            return task

    async def method_accumulative(self, key, stable_data=None, data=None, join_func=None,
                                  sender=None, wait=Wait.YES):
        """ Execute vk method `key` with static data `stable_data` and accumulative data `data` (data appends to
        already set data with function `join_func`) with sender settings `sender`, with waiting settings `wait`
        """

        client = self.get_current_sender(key, sender)

        if not client:
            self.logger.error(f"No executor for \"{key}\"!")
            return False

        a_task = None

        i = 0
        while i < client.queue.requests:
            req = client.queue.get_nowait()

            if isinstance(req, RequestAccumulative):
                for k, v in stable_data.items():
                    if req.data[k] != v:
                        break

                else:
                    for k, v in data.items():
                        if v in req.data[k]:
                            break

                    else:
                        a_task = req

            await client.queue.enqueue(req)

            if a_task:
                break

            i += 1

        if a_task is None:
            full_data = stable_data
            for k, v in data.items():
                full_data[k] = ""

            a_task = RequestAccumulative(key, full_data, sender, join_func)

            await client.queue.enqueue(a_task)

        task = a_task.accumulate(data)

        if wait == Wait.NO:
            return None

        elif wait == Wait.YES:
            try:
                return await asyncio.wait_for(task, 60)
            except asyncio.CancelledError:
                pass

            except Exception:
                import traceback
                traceback.print_exc()

        elif wait == Wait.CUSTOM:
            return task

    def get_current_sender(self, key, sender=None):
        """Get group or user for executing method `key` with sender settings `sender`"""

        if sender is None:
            sender = self.get_default_sender(key)

        if self.vk_users and sender.user:
            self.current_user += 1
            return self.vk_users[(self.current_user if sender.target is None else sender.target) % len(self.vk_users)]

        elif self.vk_groups and sender.group:
            self.current_token += 1
            return self.vk_groups[(self.current_token if sender.target is None else sender.target) %
                                  len(self.vk_groups)]

        return None

    def get_default_sender(self, key):
        """Get sender settings for method `key`"""

        if self.group and is_available_from_group(key):
            sender = Sender(group=True, target=self.current_token)

        elif is_available_from_public(key):
            sender = Sender(user=True, target=self.current_user)

        else:
            sender = Sender(user=True, target=self.current_user)

        return sender

    def stop(self):
        """Method for cleaning"""

        for api in self.vk_users:
            api.stop()

        for api in self.vk_groups:
            api.stop()
