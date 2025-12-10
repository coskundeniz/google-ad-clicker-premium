import os
import random
import shutil
import sys
import tempfile
from pathlib import Path
from time import sleep
from typing import Optional, Union

try:
    import pyautogui
    import requests
    import seleniumbase
    import undetected_chromedriver

except ImportError:
    packages_path = Path.cwd() / "env" / "Lib" / "site-packages"
    sys.path.insert(0, f"{packages_path}")

    import pyautogui
    import requests
    import seleniumbase
    import undetected_chromedriver

from config_reader import config
from geolocation_db import GeolocationDB
from logger import logger
from proxy import install_plugin
from utils import get_location, get_locale_language, get_random_sleep


IS_POSIX = sys.platform.startswith(("cygwin", "linux"))


class CustomChrome(undetected_chromedriver.Chrome):
    """Modified Chrome implementation"""

    def quit(self):

        try:
            # logger.debug("Terminating the browser")
            os.kill(self.browser_pid, 15)
            if IS_POSIX:
                os.waitpid(self.browser_pid, 0)
            else:
                sleep(0.05 * config.behavior.wait_factor)
        except (AttributeError, ChildProcessError, RuntimeError, OSError):
            pass
        except TimeoutError as e:
            logger.debug(e, exc_info=True)
        except Exception:
            pass

        if hasattr(self, "service") and getattr(self.service, "process", None):
            # logger.debug("Stopping webdriver service")
            self.service.stop()

        try:
            if self.reactor:
                # logger.debug("Shutting down Reactor")
                self.reactor.event.set()
        except Exception:
            pass

        if (
            hasattr(self, "keep_user_data_dir")
            and hasattr(self, "user_data_dir")
            and not self.keep_user_data_dir
        ):
            for _ in range(5):
                try:
                    shutil.rmtree(self.user_data_dir, ignore_errors=False)
                except FileNotFoundError:
                    pass
                except (RuntimeError, OSError, PermissionError) as e:
                    logger.debug(
                        "When removing the temp profile, a %s occured: %s\nretrying..."
                        % (e.__class__.__name__, e)
                    )
                else:
                    # logger.debug("successfully removed %s" % self.user_data_dir)
                    break

                sleep(0.1 * config.behavior.wait_factor)

        # dereference patcher, so patcher can start cleaning up as well.
        # this must come last, otherwise it will throw 'in use' errors
        self.patcher = None

    def __del__(self):
        try:
            self.service.process.kill()
        except Exception:  # noqa
            pass

        try:
            self.quit()
        except OSError:
            pass

    @classmethod
    def _ensure_close(cls, self):
        # needs to be a classmethod so finalize can find the reference
        if (
            hasattr(self, "service")
            and hasattr(self.service, "process")
            and hasattr(self.service.process, "kill")
        ):
            self.service.process.kill()

            if IS_POSIX:
                try:
                    # prevent zombie processes
                    os.waitpid(self.service.process.pid, 0)
                except ChildProcessError:
                    pass
                except Exception:
                    pass
            else:
                sleep(0.05 * config.behavior.wait_factor)


def create_webdriver(
    proxy: str, user_agent: Optional[str] = None, plugin_folder_name: Optional[str] = None
) -> tuple[undetected_chromedriver.Chrome, Optional[str]]:
    """Create Selenium Chrome webdriver instance

    :type proxy: str
    :param proxy: Proxy to use in ip:port or user:pass@host:port format
    :type user_agent: str
    :param user_agent: User agent string
    :type plugin_folder_name: str
    :param plugin_folder_name: Plugin folder name for proxy
    :rtype: tuple
    :returns: (undetected_chromedriver.Chrome, country_code) pair
    """

    if config.webdriver.use_seleniumbase:
        logger.debug("Using SeleniumBase...")
        return create_seleniumbase_driver(proxy, user_agent)

    geolocation_db_client = GeolocationDB()

    chrome_options = undetected_chromedriver.ChromeOptions()
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--no-first-run")
    chrome_options.add_argument("--no-service-autorun")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-infobars")
    chrome_options.add_argument("--disable-popup-blocking")
    chrome_options.add_argument("--disable-notifications")
    chrome_options.add_argument("--disable-translate")
    chrome_options.add_argument("--deny-permission-prompts")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("--disable-application-cache")
    chrome_options.add_argument("--disable-breakpad")
    chrome_options.add_argument("--disable-renderer-backgrounding")
    chrome_options.add_argument("--disable-browser-side-navigation")
    chrome_options.add_argument("--disable-save-password-bubble")
    chrome_options.add_argument("--disable-single-click-autofill")
    chrome_options.add_argument("--disable-prompt-on-repost")
    chrome_options.add_argument("--disable-backgrounding-occluded-windows")
    chrome_options.add_argument("--disable-hang-monitor")
    chrome_options.add_argument("--dns-prefetch-disable")
    chrome_options.add_argument("--allow-running-insecure-content")
    chrome_options.add_argument("--disable-search-engine-choice-screen")
    chrome_options.add_argument(f"--user-agent={user_agent}")

    if IS_POSIX:
        chrome_options.add_argument("--disable-setuid-sandbox")

    disabled_features = [
        "OptimizationGuideModelDownloading",
        "OptimizationHintsFetching",
        "OptimizationTargetPrediction",
        "OptimizationHints",
        "Translate",
        "DownloadBubble",
        "DownloadBubbleV2",
        "PrivacySandboxSettings4",
        "UserAgentClientHint",
        "DisableLoadExtensionCommandLineSwitch",
    ]
    chrome_options.add_argument(f"--disable-features={','.join(disabled_features)}")

    # disable WebRTC IP tracking
    webrtc_preferences = {
        "webrtc.ip_handling_policy": "disable_non_proxied_udp",
        "webrtc.multiple_routes_enabled": False,
        "webrtc.nonproxied_udp_enabled": False,
    }
    chrome_options.add_experimental_option("prefs", webrtc_preferences)

    if config.webdriver.incognito:
        chrome_options.add_argument("--incognito")

    base_dir = Path(tempfile.gettempdir()) / "uc_profiles"
    base_dir.mkdir(exist_ok=True)
    profile_dir = base_dir / f"profile_{random.randint(1000, 9999)}"

    chrome_options.add_argument(f"--user-data-dir={profile_dir}")
    chrome_options.add_argument("--profile-directory=Default")

    country_code = None

    multi_browser_flag_file = Path(".MULTI_BROWSERS_IN_USE")
    multi_procs_enabled = multi_browser_flag_file.exists()
    driver_exe_path = None

    if multi_procs_enabled:
        driver_exe_path = _get_driver_exe_path()

    if proxy:
        if config.webdriver.auth:
            if "@" not in proxy or proxy.count(":") != 2:
                raise ValueError(
                    "Invalid proxy format! Should be in 'username:password@host:port' format"
                )

            username, password = proxy.split("@")[0].split(":")
            host, port = proxy.split("@")[1].split(":")

            masked_username = username[:3] + "***" + username[-3:] if len(username) > 6 else "***"
            masked_password = password[:3] + "***" + password[-3:] if len(password) > 6 else "***"
            masked_proxy = f"{masked_username}:{masked_password}@{host}:{port}"

            logger.info(f"Using proxy: {masked_proxy}")
            logger.debug(f"Using proxy: {proxy}")

            install_plugin(chrome_options, host, int(port), username, password, plugin_folder_name)
            sleep(2 * config.behavior.wait_factor)
        else:
            logger.info(f"Using proxy: {proxy}")
            chrome_options.add_argument(f"--proxy-server={proxy}")

        # get location of the proxy IP
        lat, long, country_code, timezone = get_location(geolocation_db_client, proxy)

        if config.webdriver.language_from_proxy:
            lang = get_locale_language(country_code)
            chrome_options.add_experimental_option("prefs", {"intl.accept_languages": str(lang)})
            chrome_options.add_argument(f"--lang={lang[:2]}")

        driver = CustomChrome(
            driver_executable_path=(
                driver_exe_path if multi_procs_enabled and Path(driver_exe_path).exists() else None
            ),
            options=chrome_options,
            user_multi_procs=multi_procs_enabled,
            use_subprocess=False,
        )

        accuracy = 95

        # set geolocation and timezone of the browser according to IP address
        if lat and long:
            driver.execute_cdp_cmd(
                "Emulation.setGeolocationOverride",
                {"latitude": lat, "longitude": long, "accuracy": accuracy},
            )

            if not timezone:
                response = requests.get(f"http://timezonefinder.michelfe.it/api/0_{long}_{lat}")

                if response.status_code == 200:
                    timezone = response.json()["tz_name"]

            driver._custom_timezone = timezone

            driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": timezone})

            logger.debug(
                f"Timezone of {proxy.split('@')[1] if config.webdriver.auth else proxy}: {timezone}"
            )

    else:
        driver = CustomChrome(
            driver_executable_path=(
                driver_exe_path if multi_procs_enabled and Path(driver_exe_path).exists() else None
            ),
            options=chrome_options,
            user_multi_procs=multi_procs_enabled,
            use_subprocess=False,
        )

    if config.webdriver.window_size:
        width, height = config.webdriver.window_size.split(",")
        logger.debug(f"Setting window size as {width}x{height} px")
        driver.set_window_size(width, height)
    else:
        logger.debug("Maximizing window...")
        driver.maximize_window()

    if config.webdriver.shift_windows:
        width, height = (
            config.webdriver.window_size.split(",")
            if config.webdriver.window_size
            else (None, None)
        )
        _shift_window_position(driver, width, height)

    return (driver, country_code) if config.webdriver.country_domain else (driver, None)


def create_seleniumbase_driver(
    proxy: str, user_agent: Optional[str] = None
) -> tuple[seleniumbase.Driver, Optional[str]]:
    """Create SeleniumBase Chrome webdriver instance

    :type proxy: str
    :param proxy: Proxy to use in ip:port or user:pass@host:port format
    :type user_agent: str
    :param user_agent: User agent string
    :rtype: tuple
    :returns: (Driver, country_code) pair
    """

    geolocation_db_client = GeolocationDB()

    country_code = None

    if proxy:
        if config.webdriver.auth:
            if "@" not in proxy or proxy.count(":") != 2:
                raise ValueError(
                    "Invalid proxy format! Should be in 'username:password@host:port' format"
                )

            username, password = proxy.split("@")[0].split(":")
            host, port = proxy.split("@")[1].split(":")

            masked_username = username[:3] + "***" + username[-3:] if len(username) > 6 else "***"
            masked_password = password[:3] + "***" + password[-3:] if len(password) > 6 else "***"
            masked_proxy = f"{masked_username}:{masked_password}@{host}:{port}"

            logger.info(f"Using proxy: {masked_proxy}")
            logger.debug(f"Using proxy: {proxy}")
        else:
            logger.info(f"Using proxy: {proxy}")

        # get location of the proxy IP
        lat, long, country_code, timezone = get_location(geolocation_db_client, proxy)

        if config.webdriver.language_from_proxy:
            lang = get_locale_language(country_code)

    base_dir = Path(tempfile.gettempdir()) / "sb_profiles"
    base_dir.mkdir(exist_ok=True)
    profile_dir = base_dir / f"profile_{random.randint(1000,9999)}"

    driver = seleniumbase.get_driver(
        browser_name="chrome",
        undetectable=True,
        headless2=False,
        do_not_track=True,
        user_agent=user_agent,
        proxy_string=proxy or None,
        multi_proxy=config.behavior.browser_count > 1,
        incognito=config.webdriver.incognito,
        locale_code=str(lang) if config.webdriver.language_from_proxy else None,
        user_data_dir=str(profile_dir),
    )

    # set geolocation and timezone if available
    if proxy and lat and long:
        accuracy = 95
        driver.execute_cdp_cmd(
            "Emulation.setGeolocationOverride",
            {"latitude": lat, "longitude": long, "accuracy": accuracy},
        )

        if not timezone:
            response = requests.get(f"http://timezonefinder.michelfe.it/api/0_{long}_{lat}")
            if response.status_code == 200:
                timezone = response.json()["tz_name"]

        driver._custom_timezone = timezone

        driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": timezone})

        logger.debug(
            f"Timezone of {proxy.split('@')[1] if config.webdriver.auth else proxy}: {timezone}"
        )

    # handle window size and position
    if config.webdriver.window_size:
        width, height = config.webdriver.window_size.split(",")
        logger.debug(f"Setting window size as {width}x{height} px")
        driver.set_window_size(int(width), int(height))
    else:
        logger.debug("Maximizing window...")
        driver.maximize_window()

    if config.webdriver.shift_windows:
        width, height = (
            config.webdriver.window_size.split(",")
            if config.webdriver.window_size
            else (None, None)
        )
        _shift_window_position(driver, width, height)

    return (driver, country_code) if config.webdriver.country_domain else (driver, None)


def _shift_window_position(
    driver: Union[undetected_chromedriver.Chrome, seleniumbase.Driver],
    width: int = None,
    height: int = None,
) -> None:
    """Shift the browser window position randomly

    :type driver: Union[undetected_chromedriver.Chrome, seleniumbase.Driver]
    :param driver: WebDriver instance
    :type width: int
    :param width: Predefined window width
    :type height: int
    :param height: Predefined window height
    """

    # get screen size
    screen_width, screen_height = pyautogui.size()

    window_position = driver.get_window_position()
    x, y = window_position["x"], window_position["y"]

    random_x_offset = random.choice(range(150, 300))
    random_y_offset = random.choice(range(75, 150))

    if width and height:
        new_width = int(width) - random_x_offset
        new_height = int(height) - random_y_offset
    else:
        new_width = int(screen_width * 2 / 3) - random_x_offset
        new_height = int(screen_height * 2 / 3) - random_y_offset

    # set the window size and position
    driver.set_window_size(new_width, new_height)

    new_x = min(x + random_x_offset, screen_width - new_width)
    new_y = min(y + random_y_offset, screen_height - new_height)

    logger.debug(f"Setting window position as ({new_x},{new_y})...")

    driver.set_window_position(new_x, new_y)
    sleep(get_random_sleep(0.1, 0.5) * config.behavior.wait_factor)


def _get_driver_exe_path() -> str:
    """Get the path for the chromedriver executable to avoid downloading and patching each time

    :rtype: str
    :returns: Absoulute path of the chromedriver executable
    """

    platform = sys.platform
    prefix = "undetected"
    exe_name = "chromedriver%s"

    if platform.endswith("win32"):
        exe_name %= ".exe"
    if platform.endswith(("linux", "linux2")):
        exe_name %= ""
    if platform.endswith("darwin"):
        exe_name %= ""

    if platform.endswith("win32"):
        dirpath = "~/appdata/roaming/undetected_chromedriver"
    elif "LAMBDA_TASK_ROOT" in os.environ:
        dirpath = "/tmp/undetected_chromedriver"
    elif platform.startswith(("linux", "linux2")):
        dirpath = "~/.local/share/undetected_chromedriver"
    elif platform.endswith("darwin"):
        dirpath = "~/Library/Application Support/undetected_chromedriver"
    else:
        dirpath = "~/.undetected_chromedriver"

    driver_exe_folder = os.path.abspath(os.path.expanduser(dirpath))
    driver_exe_path = os.path.join(driver_exe_folder, "_".join([prefix, exe_name]))

    return driver_exe_path


def execute_stealth_js_code(driver: Union[undetected_chromedriver.Chrome, seleniumbase.Driver]):
    """Execute the stealth JS code to prevent detection

    Signature changes can be tested by loading the following addresses
    - https://browserleaks.com/canvas
    - https://browserleaks.com/webrtc
    - https://browserleaks.com/webgl

    For bot check
    - https://pixelscan.net/bot-check
    - https://www.browserscan.net/
    - https://bot.sannysoft.com/

    :type driver: Union[undetected_chromedriver.Chrome, seleniumbase.Driver]
    :param driver: WebDriver instance
    """

    # timezone spoofing and normalization
    timezone = getattr(driver, "_custom_timezone", None)

    if timezone:
        driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": timezone})

        timezone_js = f"""
        (() => {{
            const tz = "{timezone}";
            const getOffset = (tzName) => {{
                try {{
                    const now = new Date();
                    const local = new Date(now.toLocaleString("en-US", {{ timeZone: tzName }}));
                    const utc = new Date(now.toLocaleString("en-US", {{ timeZone: "UTC" }}));
                    return (utc - local) / 60000; // minutes
                }} catch (e) {{
                    return 0;
                }}
            }};
            const offset = getOffset(tz);
            const sign = offset <= 0 ? "+" : "-";
            const absOffset = Math.abs(offset);
            const hours = String(Math.floor(absOffset / 60)).padStart(2, "0");
            const minutes = String(Math.abs(offset) % 60).padStart(2, "0");
            const gmtString = `GMT${{sign}}${{hours}}${{minutes}}`;

            // Patch Intl
            const origIntl = Intl.DateTimeFormat.prototype.resolvedOptions;
            Intl.DateTimeFormat.prototype.resolvedOptions = function() {{
                const opts = origIntl.call(this);
                opts.timeZone = tz;
                return opts;
            }};

            // Patch Date
            const origOffset = Date.prototype.getTimezoneOffset;
            Date.prototype.getTimezoneOffset = function() {{ return offset; }};

            const origToString = Date.prototype.toString;
            Date.prototype.toString = function() {{
                const str = origToString.call(this);
                return str.replace(/GMT[+-]\\d{{4}}.*$/, `${{gmtString}} (${{tz}})`);
            }};

            const origLocale = Date.prototype.toLocaleString;
            Date.prototype.toLocaleString = function(...args) {{
                const opts = args[1] || {{}};
                if (!opts.timeZone) opts.timeZone = tz;
                return origLocale.call(this, args[0] || undefined, opts);
            }};

            // Hide modifications
            const fakeNative = (n) => `function ${{n}}() {{ [native code] }}`;
            [
                Date.prototype.getTimezoneOffset,
                Date.prototype.toString,
                Date.prototype.toLocaleString,
                Intl.DateTimeFormat.prototype.resolvedOptions
            ].forEach(fn => {{
                if (fn && fn.name)
                    Object.defineProperty(fn, "toString", {{ value: () => fakeNative(fn.name) }});
            }});

            // Proxy Intl.DateTimeFormat constructor for consistency
            Object.defineProperty(Intl, "DateTimeFormat", {{
                value: new Proxy(Intl.DateTimeFormat, {{
                    construct(target, args) {{
                        if (args[1] && args[1].timeZone)
                            args[1].timeZone = tz;
                        return new target(...args);
                    }}
                }})
            }});
        }})();
        """
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": timezone_js})

    # DevTools detection prevention
    devtools_evasion_js = """
    (function() {
        const realInnerWidth = window.innerWidth;
        const realInnerHeight = window.innerHeight;

        // The key: outerHeight should be VERY CLOSE to innerHeight (browser closed)
        // Not random, but consistent small difference
        try {
            Object.defineProperty(window, 'outerHeight', {
                get: function() {
                    return realInnerHeight + 39;
                },
                configurable: true
            });

            Object.defineProperty(window, 'outerWidth', {
                get: function() {
                    return realInnerWidth + 12;
                },
                configurable: true
            });
        } catch(e) {}

        // 2. Override innerWidth/innerHeight to be stable
        try {
            Object.defineProperty(window, 'innerWidth', {
                get: function() {
                    return realInnerWidth;
                },
                configurable: true
            });

            Object.defineProperty(window, 'innerHeight', {
                get: function() {
                    return realInnerHeight;
                },
                configurable: true
            });
        } catch(e) {}

        // 3. Override screen properties
        try {
            Object.defineProperty(screen, 'availWidth', {
                get: () => screen.width,
                configurable: true
            });
            Object.defineProperty(screen, 'availHeight', {
                get: () => screen.height,
                configurable: true
            });
        } catch(e) {}

        // 4. Remove debugger detection
        Object.defineProperty(window, 'devtools', {
            get: () => undefined,
            set: () => {},
            configurable: false
        });

        // 5. Override console methods
        const noop = () => {};
        ['log', 'debug', 'info', 'warn', 'error'].forEach(m => {
            console[m] = noop;
        });

        // 6. Block Function toString inspection
        const OriginalToString = Function.prototype.toString;
        Function.prototype.toString = function() {
            if (this === Function.prototype.toString) {
                return 'function toString() { [native code] }';
            }
            return 'function() { [native code] }';
        };

        // 7. Prevent Error.stack inspection
        const OriginalError = Error;
        window.Error = function(...args) {
            const error = new OriginalError(...args);
            if (error.stack) {
                error.stack = error.stack.split('\\n').slice(0, 2).join('\\n');
            }
            return error;
        };

        // 8. Block debugger statement
        window.eval = new Proxy(window.eval, {
            apply(target, thisArg, args) {
                if (args[0] && args[0].includes('debugger')) {
                    return undefined;
                }
                return Reflect.apply(target, thisArg, args);
            }
        });

    })();
    """
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": devtools_evasion_js})

    # navigator.plugins evasion
    plugins_js = """
    (function() {
        // Save original PluginArray and MimeTypeArray constructors
        const OriginalPluginArray = PluginArray;
        const OriginalMimeTypeArray = MimeTypeArray;

        // Create MimeType objects
        const createMimeType = (type, suffixes, description, plugin) => {
            const mimeType = {
                type: type,
                suffixes: suffixes,
                description: description,
                enabledPlugin: plugin
            };
            return mimeType;
        };

        // Create Plugin objects
        const createPlugin = (name, description, filename, mimeTypes) => {
            const plugin = {
                name: name,
                description: description,
                filename: filename,
                length: mimeTypes.length
            };

            mimeTypes.forEach((mimeType, index) => {
                plugin[index] = createMimeType(
                    mimeType.type,
                    mimeType.suffixes,
                    mimeType.description,
                    plugin
                );
            });

            plugin.item = function(index) {
                return this[index] || null;
            };

            plugin.namedItem = function(name) {
                for (let i = 0; i < this.length; i++) {
                    if (this[i].type === name) return this[i];
                }
                return null;
            };

            return plugin;
        };

        // Create plugin data
        const pluginsData = [
            createPlugin('PDF Viewer', 'Portable Document Format', 'internal-pdf-viewer', [
                { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' }
            ]),
            createPlugin('Chrome PDF Viewer', 'Portable Document Format', 'internal-pdf-viewer', [
                { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' }
            ]),
            createPlugin('Chromium PDF Viewer', 'Portable Document Format', 'internal-pdf-viewer', [
                { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' }
            ]),
            createPlugin('Microsoft Edge PDF Viewer', 'Portable Document Format', 'internal-pdf-viewer', [
                { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' }
            ]),
            createPlugin('WebKit built-in PDF', 'Portable Document Format', 'internal-pdf-viewer', [
                { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' }
            ])
        ];

        // Create a real PluginArray instance by extending it
        class FakePluginArray extends OriginalPluginArray {
            constructor(plugins) {
                super();
                plugins.forEach((plugin, index) => {
                    this[index] = plugin;
                    this[plugin.name] = plugin;
                });

                // Override length
                Object.defineProperty(this, 'length', {
                    get: () => plugins.length,
                    enumerable: false
                });
            }

            item(index) {
                return this[index] || null;
            }

            namedItem(name) {
                return this[name] || null;
            }

            refresh() {}
        }

        // Create the plugin array instance
        const pluginArray = new FakePluginArray(pluginsData);

        // Make methods look native
        ['item', 'namedItem', 'refresh'].forEach(method => {
            Object.defineProperty(pluginArray[method], 'toString', {
                value: () => `function ${method}() { [native code] }`,
                writable: false,
                configurable: false
            });
        });

        // Create MimeTypeArray
        class FakeMimeTypeArray extends OriginalMimeTypeArray {
            constructor(plugins) {
                super();
                let mimeIndex = 0;

                plugins.forEach(plugin => {
                    for (let i = 0; i < plugin.length; i++) {
                        this[mimeIndex] = plugin[i];
                        this[plugin[i].type] = plugin[i];
                        mimeIndex++;
                    }
                });

                Object.defineProperty(this, 'length', {
                    get: () => mimeIndex,
                    enumerable: false
                });
            }

            item(index) {
                return this[index] || null;
            }

            namedItem(name) {
                return this[name] || null;
            }
        }

        const mimeTypesArray = new FakeMimeTypeArray(pluginsData);

        // Make methods look native
        ['item', 'namedItem'].forEach(method => {
            Object.defineProperty(mimeTypesArray[method], 'toString', {
                value: () => `function ${method}() { [native code] }`,
                writable: false,
                configurable: false
            });
        });

        // Override navigator.plugins and navigator.mimeTypes
        Object.defineProperty(navigator, 'plugins', {
            get: () => pluginArray,
            enumerable: true,
            configurable: true
        });

        Object.defineProperty(navigator, 'mimeTypes', {
            get: () => mimeTypesArray,
            enumerable: true,
            configurable: true
        });

    })();
    """
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": plugins_js})

    # iframe.contentWindow evasion
    iframe_js = """
    try {
        const defaultGetter = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow').get;
        Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
            get: function() {
                const win = defaultGetter.call(this);
                if (!win) return win;

                try {
                    const proxy = new Proxy(win, {
                        get: (target, prop) => {
                            if (prop === 'self' || prop === 'window' || prop === 'parent' || prop === 'top') {
                                return proxy;
                            }
                            return Reflect.get(target, prop);
                        },
                        has: (target, prop) => {
                            if (prop === 'webdriver') return false;
                            return Reflect.has(target, prop);
                        }
                    });
                    return proxy;
                } catch(e) {
                    return win;
                }
            },
            configurable: true,
            enumerable: true
        });
    } catch(e) {}
    """
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": iframe_js})

    # media codecs evasion
    media_codecs_js = """
    const originalCanPlayType = HTMLMediaElement.prototype.canPlayType;
    HTMLMediaElement.prototype.canPlayType = function(type) {
        if (type === 'video/mp4; codecs="avc1.42E01E"') return 'probably';
        if (type === 'audio/mpeg') return 'probably';
        if (type === 'audio/mp4; codecs="mp4a.40.2"') return 'probably';
        return originalCanPlayType.apply(this, arguments);
    };

    Object.defineProperty(HTMLMediaElement.prototype.canPlayType, 'toString', {
        value: () => 'function canPlayType() { [native code] }'
    });
    """
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": media_codecs_js})

    # canvas fingerprint randomization
    canvas_js = """
    // Generate consistent but random noise seed
    const noiseSeed = Math.random() * 10;

    const noisify = (canvas, context) => {
        const shift = {
            r: Math.floor(noiseSeed * 2) - 1,
            g: Math.floor(noiseSeed * 2) - 1,
            b: Math.floor(noiseSeed * 2) - 1,
            a: Math.floor(noiseSeed * 2) - 1
        };

        const width = canvas.width;
        const height = canvas.height;

        if (width > 0 && height > 0) {
            try {
                const imageData = context.getImageData(0, 0, width, height);
                for (let i = 0; i < imageData.data.length; i += 4) {
                    imageData.data[i + 0] = imageData.data[i + 0] + shift.r;
                    imageData.data[i + 1] = imageData.data[i + 1] + shift.g;
                    imageData.data[i + 2] = imageData.data[i + 2] + shift.b;
                    imageData.data[i + 3] = imageData.data[i + 3] + shift.a;
                }
                context.putImageData(imageData, 0, 0);
            } catch(e) {}
        }
    };

    const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
    const originalToBlob = HTMLCanvasElement.prototype.toBlob;
    const originalGetImageData = CanvasRenderingContext2D.prototype.getImageData;

    HTMLCanvasElement.prototype.toDataURL = function(...args) {
        const context = this.getContext('2d');
        if (context) noisify(this, context);
        return originalToDataURL.apply(this, args);
    };

    HTMLCanvasElement.prototype.toBlob = function(...args) {
        const context = this.getContext('2d');
        if (context) noisify(this, context);
        return originalToBlob.apply(this, args);
    };

    // Protect toString
    Object.defineProperty(HTMLCanvasElement.prototype.toDataURL, 'toString', {
        value: () => 'function toDataURL() { [native code] }'
    });
    Object.defineProperty(HTMLCanvasElement.prototype.toBlob, 'toString', {
        value: () => 'function toBlob() { [native code] }'
    });
    """
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": canvas_js})

    # WebGL vendor/renderer randomization (enhanced)
    webgl_js = """
    // Hardware-based vendor/renderer pairs only (avoid SwiftShader/Google to prevent detection)
    const webglData = [
        { vendor: 'Intel Inc.', renderer: 'ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)' },
        { vendor: 'NVIDIA Corporation', renderer: 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)' },
        { vendor: 'AMD', renderer: 'ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)' },
        { vendor: 'Intel Inc.', renderer: 'ANGLE (Intel, Intel(R) Iris(TM) Graphics 6100 Direct3D11 vs_5_0 ps_5_0, D3D11)' },
        { vendor: 'NVIDIA Corporation', renderer: 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)' }
    ];

    const selected = webglData[Math.floor(Math.random() * webglData.length)];
    const vendor = selected.vendor;
    const renderer = selected.renderer;

    // Override getParameter for WebGLRenderingContext
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(parameter) {
        // Unmasked vendor/renderer
        if (parameter === 37445) return vendor;
        if (parameter === 37446) return renderer;
        // Standard vendor/renderer
        if (parameter === 33901) return vendor;
        if (parameter === 33902) return renderer;
        // Version info
        if (parameter === 7938) return 'WebGL 1.0 (OpenGL ES 2.0 Chromium)';
        if (parameter === 35724) return 'WebGL GLSL ES 1.00 (OpenGL ES GLSL ES 1.0 Chromium)';
        // Max texture size
        if (parameter === 3379) return 16384 + Math.floor(Math.random() * 1024);
        // Other parameters
        if (parameter === 34076) return 16;
        if (parameter === 34930) return 16;
        if (parameter === 36349) return 32;
        return getParameter.apply(this, arguments);
    };

    // Same for WebGL2RenderingContext
    if (window.WebGL2RenderingContext) {
        const getParameter2 = WebGL2RenderingContext.prototype.getParameter;
        WebGL2RenderingContext.prototype.getParameter = function(parameter) {
            if (parameter === 37445) return vendor;
            if (parameter === 37446) return renderer;
            if (parameter === 33901) return vendor;
            if (parameter === 33902) return renderer;
            if (parameter === 7938) return 'WebGL 2.0 (OpenGL ES 3.0 Chromium)';
            if (parameter === 35724) return 'WebGL GLSL ES 3.00 (OpenGL ES GLSL ES 3.0 Chromium)';
            if (parameter === 3379) return 16384 + Math.floor(Math.random() * 1024);
            if (parameter === 34076) return 16;
            if (parameter === 34930) return 16;
            if (parameter === 36349) return 32;
            return getParameter2.apply(this, arguments);
        };

        Object.defineProperty(WebGL2RenderingContext.prototype.getParameter, 'toString', {
            value: () => 'function getParameter() { [native code] }'
        });
    }

    // Make it look native
    Object.defineProperty(WebGLRenderingContext.prototype.getParameter, 'toString', {
        value: () => 'function getParameter() { [native code] }'
    });
    """
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": webgl_js})

    # WebRTC blocking
    webrtc_js = """
    (function() {
        // 1. Completely disable RTCPeerConnection
        window.RTCPeerConnection = undefined;
        window.webkitRTCPeerConnection = undefined;
        window.mozRTCPeerConnection = undefined;

        // 2. Disable RTCDataChannel
        window.RTCDataChannel = undefined;

        // 3. Block getUserMedia
        if (navigator.mediaDevices) {
            navigator.mediaDevices.getUserMedia = () => Promise.reject(new Error('Permission denied'));
            navigator.mediaDevices.getDisplayMedia = () => Promise.reject(new Error('Permission denied'));
            navigator.mediaDevices.enumerateDevices = () => Promise.resolve([]);
        }

        // 4. Block legacy getUserMedia
        if (navigator.getUserMedia) {
            navigator.getUserMedia = (c, s, e) => e(new Error('Permission denied'));
        }

        // 5. Block webkitGetUserMedia
        if (navigator.webkitGetUserMedia) {
            navigator.webkitGetUserMedia = (c, s, e) => e(new Error('Permission denied'));
        }

        // 6. Disable getStats
        if (window.RTCPeerConnection) {
            window.RTCPeerConnection.prototype.getStats = undefined;
        }

        // 7. Disable createDataChannel
        if (window.RTCPeerConnection) {
            window.RTCPeerConnection.prototype.createDataChannel = undefined;
        }
    })();
    """
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": webrtc_js})

    logger.debug("Applied advanced stealth JavaScript techniques")
