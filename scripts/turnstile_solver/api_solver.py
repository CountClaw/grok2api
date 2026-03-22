import os
import sys
import time
import uuid
import random
import logging
import asyncio
from typing import Dict, List, Optional, Union
import argparse
from quart import Quart, request, jsonify
try:
    from camoufox.async_api import AsyncCamoufox
except Exception:  # pragma: no cover
    AsyncCamoufox = None  # type: ignore

try:
    from patchright.async_api import async_playwright
except Exception:  # pragma: no cover
    from playwright.async_api import async_playwright
from db_results import init_db, save_result, load_result, cleanup_old_results
from browser_configs import browser_config
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.align import Align
from rich import box



COLORS = {
    'MAGENTA': '\033[35m',
    'BLUE': '\033[34m',
    'GREEN': '\033[32m',
    'YELLOW': '\033[33m',
    'RED': '\033[31m',
    'RESET': '\033[0m',
}


class CustomLogger(logging.Logger):
    @staticmethod
    def format_message(level, color, message):
        timestamp = time.strftime('%H:%M:%S')
        return f"[{timestamp}] [{COLORS.get(color)}{level}{COLORS.get('RESET')}] -> {message}"

    def debug(self, message, *args, **kwargs):
        super().debug(self.format_message('DEBUG', 'MAGENTA', message), *args, **kwargs)

    def info(self, message, *args, **kwargs):
        super().info(self.format_message('INFO', 'BLUE', message), *args, **kwargs)

    def success(self, message, *args, **kwargs):
        super().info(self.format_message('SUCCESS', 'GREEN', message), *args, **kwargs)

    def warning(self, message, *args, **kwargs):
        super().warning(self.format_message('WARNING', 'YELLOW', message), *args, **kwargs)

    def error(self, message, *args, **kwargs):
        super().error(self.format_message('ERROR', 'RED', message), *args, **kwargs)


logging.setLoggerClass(CustomLogger)
logger: CustomLogger = logging.getLogger("TurnstileAPIServer")  # type: ignore
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler(sys.stdout)
logger.addHandler(handler)


class TurnstileAPIServer:

    def __init__(self, headless: bool, useragent: Optional[str], debug: bool, browser_type: str, thread: int, proxy_support: bool, use_random_config: bool = False, browser_name: Optional[str] = None, browser_version: Optional[str] = None):
        self.app = Quart(__name__)
        self.debug = debug
        self.browser_type = browser_type
        self.headless = headless
        self.thread_count = thread
        self.proxy_support = proxy_support
        self.browser_pool = asyncio.Queue()
        self.use_random_config = use_random_config
        self.browser_name = browser_name
        self.browser_version = browser_version
        self.console = Console()
        
        # Initialize useragent and sec_ch_ua attributes
        self.useragent = useragent
        self.sec_ch_ua = None
        
        
        if self.browser_type in ['chromium', 'chrome', 'msedge']:
            if browser_name and browser_version:
                config = browser_config.get_browser_config(browser_name, browser_version)
                if config:
                    useragent, sec_ch_ua = config
                    self.useragent = useragent
                    self.sec_ch_ua = sec_ch_ua
            elif useragent:
                self.useragent = useragent
            else:
                browser, version, useragent, sec_ch_ua = browser_config.get_random_browser_config(self.browser_type)
                self.browser_name = browser
                self.browser_version = version
                self.useragent = useragent
                self.sec_ch_ua = sec_ch_ua
        
        self.browser_args = []
        if self.useragent:
            self.browser_args.append(f"--user-agent={self.useragent}")

        self._setup_routes()

    def display_welcome(self):
        """Displays welcome screen with logo."""
        self.console.clear()
        
        combined_text = Text()
        combined_text.append("\nChannel: ", style="bold white")
        combined_text.append("https://t.me/D3_vin", style="cyan")
        combined_text.append("\nChat: ", style="bold white")
        combined_text.append("https://t.me/D3vin_chat", style="cyan")
        combined_text.append("\nGitHub: ", style="bold white")
        combined_text.append("https://github.com/D3-vin", style="cyan")
        combined_text.append("\nVersion: ", style="bold white")
        combined_text.append("1.2a", style="green")
        combined_text.append("\n")

        info_panel = Panel(
            Align.left(combined_text),
            title="[bold blue]Turnstile Solver[/bold blue]",
            subtitle="[bold magenta]Dev by D3vin[/bold magenta]",
            box=box.ROUNDED,
            border_style="bright_blue",
            padding=(0, 1),
            width=50
        )

        try:
            self.console.print(info_panel)
            self.console.print()
        except UnicodeEncodeError:
            # Fallback for Windows consoles with non-UTF8 encoding
            print("Turnstile Solver")
            print("Channel: https://t.me/D3_vin")
            print("Chat: https://t.me/D3vin_chat")
            print("GitHub: https://github.com/D3-vin")
            print("Version: 1.2a")




    def _setup_routes(self) -> None:
        """Set up the application routes."""
        self.app.before_serving(self._startup)
        self.app.route('/turnstile', methods=['GET'])(self.process_turnstile)
        self.app.route('/result', methods=['GET'])(self.get_result)
        self.app.route('/preflight', methods=['GET'])(self.preflight_signup)
        self.app.route('/signup-email', methods=['GET'])(self.signup_email)
        self.app.route('/')(self.index)
        

    async def _startup(self) -> None:
        """Initialize the browser and page pool on startup."""
        self.display_welcome()
        logger.info("Starting browser initialization")
        try:
            await init_db()
            await self._initialize_browser()
            
            # Запускаем периодическую очистку старых результатов
            asyncio.create_task(self._periodic_cleanup())
            
        except Exception as e:
            logger.error(f"Failed to initialize browser: {str(e)}")
            raise

    async def _initialize_browser(self) -> None:
        """Initialize the browser and create the page pool."""
        playwright = None
        camoufox = None

        if self.browser_type in ['chromium', 'chrome', 'msedge']:
            playwright = await async_playwright().start()
        elif self.browser_type == "camoufox":
            if AsyncCamoufox is None:
                raise RuntimeError("camoufox is not installed. Please install camoufox or use --browser_type chromium.")
            camoufox = AsyncCamoufox(headless=self.headless)

        browser_configs = []
        for _ in range(self.thread_count):
            if self.browser_type in ['chromium', 'chrome', 'msedge']:
                if self.use_random_config:
                    browser, version, useragent, sec_ch_ua = browser_config.get_random_browser_config(self.browser_type)
                elif self.browser_name and self.browser_version:
                    config = browser_config.get_browser_config(self.browser_name, self.browser_version)
                    if config:
                        useragent, sec_ch_ua = config
                        browser = self.browser_name
                        version = self.browser_version
                    else:
                        browser, version, useragent, sec_ch_ua = browser_config.get_random_browser_config(self.browser_type)
                else:
                    browser = getattr(self, 'browser_name', 'custom')
                    version = getattr(self, 'browser_version', 'custom')
                    useragent = self.useragent
                    sec_ch_ua = getattr(self, 'sec_ch_ua', '')
            else:
                # Для camoufox и других браузеров используем значения по умолчанию
                browser = self.browser_type
                version = 'custom'
                useragent = self.useragent
                sec_ch_ua = getattr(self, 'sec_ch_ua', '')

            
            browser_configs.append({
                'browser_name': browser,
                'browser_version': version,
                'useragent': useragent,
                'sec_ch_ua': sec_ch_ua
            })

        for i in range(self.thread_count):
            config = browser_configs[i]
            
            browser_args = [
                "--window-position=0,0",
                "--force-device-scale-factor=1"
            ]
            if config['useragent']:
                browser_args.append(f"--user-agent={config['useragent']}")
            
            browser = None
            if self.browser_type in ['chromium', 'chrome', 'msedge'] and playwright:
                launch_kwargs = {
                    "headless": self.headless,
                    "args": browser_args,
                }
                # Only pass `channel` for branded browsers. Playwright's bundled Chromium does not use channel.
                if self.browser_type in ["chrome", "msedge"]:
                    launch_kwargs["channel"] = self.browser_type
                browser = await playwright.chromium.launch(**launch_kwargs)
            elif self.browser_type == "camoufox" and camoufox:
                browser = await camoufox.start()

            if browser:
                await self.browser_pool.put((i+1, browser, config))

            if self.debug:
                logger.info(f"Browser {i + 1} initialized successfully with {config['browser_name']} {config['browser_version']}")

        logger.info(f"Browser pool initialized with {self.browser_pool.qsize()} browsers")
        
        if self.use_random_config:
            logger.info(f"Each browser in pool received random configuration")
        elif self.browser_name and self.browser_version:
            logger.info(f"All browsers using configuration: {self.browser_name} {self.browser_version}")
        else:
            logger.info("Using custom configuration")
            
        if self.debug:
            for i, config in enumerate(browser_configs):
                logger.debug(f"Browser {i+1} config: {config['browser_name']} {config['browser_version']}")
                logger.debug(f"Browser {i+1} User-Agent: {config['useragent']}")
                logger.debug(f"Browser {i+1} Sec-CH-UA: {config['sec_ch_ua']}")

    async def _periodic_cleanup(self):
        """Periodic cleanup of old results every hour"""
        while True:
            try:
                await asyncio.sleep(3600)
                deleted_count = await cleanup_old_results(days_old=7)
                if deleted_count > 0:
                    logger.info(f"Cleaned up {deleted_count} old results")
            except Exception as e:
                logger.error(f"Error during periodic cleanup: {e}")

    async def _antishadow_inject(self, page):
        await page.add_init_script("""
          (function() {
            const originalAttachShadow = Element.prototype.attachShadow;
            Element.prototype.attachShadow = function(init) {
              const shadow = originalAttachShadow.call(this, init);
              if (init.mode === 'closed') {
                window.__lastClosedShadowRoot = shadow;
              }
              return shadow;
            };
          })();
        """)



    async def _optimized_route_handler(self, route):
        """Оптимизированный обработчик маршрутов для экономии ресурсов."""
        url = route.request.url
        resource_type = route.request.resource_type

        allowed_types = {'document', 'script', 'xhr', 'fetch'}

        allowed_domains = [
            'challenges.cloudflare.com',
            'static.cloudflareinsights.com',
            'cloudflare.com'
        ]
        
        if resource_type in allowed_types:
            await route.continue_()
        elif any(domain in url for domain in allowed_domains):
            await route.continue_() 
        else:
            await route.abort()

    async def _block_rendering(self, page):
        """Блокировка рендеринга для экономии ресурсов"""
        await page.route("**/*", self._optimized_route_handler)

    async def _unblock_rendering(self, page):
        """Разблокировка рендеринга"""
        await page.unroute("**/*", self._optimized_route_handler)

    async def _find_turnstile_elements(self, page, index: int):
        """Умная проверка всех возможных Turnstile элементов"""
        selectors = [
            '.cf-turnstile',
            '[data-sitekey]',
            'iframe[src*="challenges.cloudflare.com"]',
            'iframe[src*="turnstile"]',
            'iframe[title*="widget"]',
            'div[id*="turnstile"]',
            'div[class*="turnstile"]'
        ]
        
        elements = []
        for selector in selectors:
            try:
                # Безопасная проверка count()
                try:
                    count = await page.locator(selector).count()
                except Exception:
                    # Если count() дает ошибку, пропускаем этот селектор
                    continue
                    
                if count > 0:
                    elements.append((selector, count))
                    if self.debug:
                        logger.debug(f"Browser {index}: Found {count} elements with selector '{selector}'")
            except Exception as e:
                if self.debug:
                    logger.debug(f"Browser {index}: Selector '{selector}' failed: {str(e)}")
                continue
        
        return elements

    async def _find_and_click_checkbox(self, page, index: int):
        """Найти и кликнуть по чекбоксу Turnstile CAPTCHA внутри iframe"""
        try:
            # Пробуем разные селекторы iframe с защитой от ошибок
            iframe_selectors = [
                'iframe[src*="challenges.cloudflare.com"]',
                'iframe[src*="turnstile"]',
                'iframe[title*="widget"]'
            ]
            
            iframe_locator = None
            for selector in iframe_selectors:
                try:
                    test_locator = page.locator(selector).first
                    # Безопасная проверка count для iframe
                    try:
                        iframe_count = await test_locator.count()
                    except Exception:
                        iframe_count = 0
                        
                    if iframe_count > 0:
                        iframe_locator = test_locator
                        if self.debug:
                            logger.debug(f"Browser {index}: Found Turnstile iframe with selector: {selector}")
                        break
                except Exception as e:
                    if self.debug:
                        logger.debug(f"Browser {index}: Iframe selector '{selector}' failed: {str(e)}")
                    continue
            
            if iframe_locator:
                try:
                    # Получаем frame из iframe
                    iframe_element = await iframe_locator.element_handle()
                    frame = await iframe_element.content_frame()
                    
                    if frame:
                        # Ищем чекбокс внутри iframe
                        checkbox_selectors = [
                            'input[type="checkbox"]',
                            '.cb-lb input[type="checkbox"]',
                            'label input[type="checkbox"]'
                        ]
                        
                        for selector in checkbox_selectors:
                            try:
                                # Полностью избегаем locator.count() в iframe - используем альтернативный подход
                                try:
                                    # Пробуем кликнуть напрямую без count проверки
                                    checkbox = frame.locator(selector).first
                                    await checkbox.click(timeout=2000)
                                    if self.debug:
                                        logger.debug(f"Browser {index}: Successfully clicked checkbox in iframe with selector '{selector}'")
                                    return True
                                except Exception as click_e:
                                    # Если прямой клик не сработал, записываем в debug но не падаем
                                    if self.debug:
                                        logger.debug(f"Browser {index}: Direct checkbox click failed for '{selector}': {str(click_e)}")
                                    continue
                            except Exception as e:
                                if self.debug:
                                    logger.debug(f"Browser {index}: Iframe checkbox selector '{selector}' failed: {str(e)}")
                                continue
                    
                        # Если нашли iframe, но не смогли кликнуть чекбокс, пробуем клик по iframe
                        try:
                            if self.debug:
                                logger.debug(f"Browser {index}: Trying to click iframe directly as fallback")
                            await iframe_locator.click(timeout=1000)
                            return True
                        except Exception as e:
                            if self.debug:
                                logger.debug(f"Browser {index}: Iframe direct click failed: {str(e)}")
                
                except Exception as e:
                    if self.debug:
                        logger.debug(f"Browser {index}: Failed to access iframe content: {str(e)}")
            
        except Exception as e:
            if self.debug:
                logger.debug(f"Browser {index}: General iframe search failed: {str(e)}")
        
        return False

    async def _try_click_strategies(self, page, index: int):
        strategies = [
            ('checkbox_click', lambda: self._find_and_click_checkbox(page, index)),
            ('direct_widget', lambda: self._safe_click(page, '.cf-turnstile', index)),
            ('iframe_click', lambda: self._safe_click(page, 'iframe[src*="turnstile"]', index)),
            ('js_click', lambda: page.evaluate("document.querySelector('.cf-turnstile')?.click()")),
            ('sitekey_attr', lambda: self._safe_click(page, '[data-sitekey]', index)),
            ('any_turnstile', lambda: self._safe_click(page, '*[class*="turnstile"]', index)),
            ('xpath_click', lambda: self._safe_click(page, "//div[@class='cf-turnstile']", index))
        ]
        
        for strategy_name, strategy_func in strategies:
            try:
                result = await strategy_func()
                if result is True or result is None:  # None означает успех для большинства стратегий
                    if self.debug:
                        logger.debug(f"Browser {index}: Click strategy '{strategy_name}' succeeded")
                    return True
            except Exception as e:
                if self.debug:
                    logger.debug(f"Browser {index}: Click strategy '{strategy_name}' failed: {str(e)}")
                continue
        
        return False

    async def _safe_click(self, page, selector: str, index: int):
        """Полностью безопасный клик с максимальной защитой от ошибок"""
        try:
            # Пробуем кликнуть напрямую без count() проверки
            locator = page.locator(selector).first
            await locator.click(timeout=1000)
            return True
        except Exception as e:
            # Логируем ошибку только в debug режиме
            if self.debug and "Can't query n-th element" not in str(e):
                logger.debug(f"Browser {index}: Safe click failed for '{selector}': {str(e)}")
            return False

    async def _read_turnstile_token(self, page):
        """从页面中尽可能多地提取 Turnstile token。"""
        return await page.evaluate("""
        () => {
            const selectors = [
                'input[name="cf-turnstile-response"]',
                'textarea[name="cf-turnstile-response"]',
                '[name="cf-turnstile-response"]',
                'input[name*="turnstile"]',
                'textarea[name*="turnstile"]',
                '[data-turnstile-token]'
            ];

            for (const selector of selectors) {
                for (const node of document.querySelectorAll(selector)) {
                    const value = String(
                        node.value ||
                        (node.getAttribute ? node.getAttribute('value') : '') ||
                        node.textContent ||
                        ''
                    ).trim();
                    if (value.length > 20) {
                        return value;
                    }
                }
            }

            const candidates = [
                window.__turnstile_token,
                window.__cf_turnstile_token,
                window.__cfTurnstileToken,
                window.__last_turnstile_token
            ];

            for (const value of candidates) {
                if (typeof value === 'string' && value.trim().length > 20) {
                    return value.trim();
                }
            }

            return '';
        }
        """)

    async def _read_turnstile_state(self, page):
        """读取页面上的 Turnstile 状态，便于失败时输出原因。"""
        return await page.evaluate("""
        () => {
            const responseNodes = Array.from(document.querySelectorAll('[name="cf-turnstile-response"]'));
            const filledCount = responseNodes.filter((node) => {
                const value = String(
                    node.value ||
                    (node.getAttribute ? node.getAttribute('value') : '') ||
                    node.textContent ||
                    ''
                ).trim();
                return value.length > 20;
            }).length;

            return {
                hasWidget: !!document.querySelector('.cf-turnstile,[data-sitekey],iframe[src*="challenges.cloudflare.com"],iframe[src*="turnstile"]'),
                iframeCount: document.querySelectorAll('iframe[src*="challenges.cloudflare.com"],iframe[src*="turnstile"]').length,
                responseCount: responseNodes.length,
                filledCount,
                lastError: String(window.__turnstile_last_error || '').trim()
            };
        }
        """)

    async def _read_body_snippet(self, page, limit: int = 600) -> str:
        try:
            return await page.evaluate(
                """(maxLength) => {
                    const text = document.body
                        ? String(document.body.innerText || document.body.textContent || "")
                        : "";
                    return text.replace(/\\s+/g, " ").trim().slice(0, maxLength);
                }""",
                limit,
            )
        except Exception:
            return ""

    async def _read_signup_state(self, page) -> Dict[str, Union[str, int, bool]]:
        title = ""
        current_url = ""
        email_count = 0
        signup_button_count = 0
        password_count = 0

        try:
            title = await page.title()
        except Exception:
            title = ""

        try:
            current_url = page.url or ""
        except Exception:
            current_url = ""

        try:
            email_count = await page.locator('input[name="email"]').count()
        except Exception:
            email_count = 0

        try:
            signup_button_count = await page.locator("button:has-text('Sign up with email')").count()
        except Exception:
            signup_button_count = 0

        try:
            password_count = await page.locator('input[name="password"]').count()
        except Exception:
            password_count = 0

        body_snippet = await self._read_body_snippet(page, limit=700)
        lowered_title = title.lower()
        lowered_body = body_snippet.lower()

        return {
            "title": title,
            "url": current_url,
            "bodySnippet": body_snippet,
            "emailInputCount": email_count,
            "signUpButtonCount": signup_button_count,
            "passwordInputCount": password_count,
            "signupReady": email_count > 0 or signup_button_count > 0 or password_count > 0,
            "challengePresent": (
                "just a moment" in lowered_title
                or "attention required" in lowered_title
                or "checking your browser" in lowered_title
                or "cloudflare" in lowered_body
            ),
        }

    def _build_context_options(self, browser_config: Dict[str, str]) -> Dict[str, Union[str, Dict[str, str]]]:
        context_options: Dict[str, Union[str, Dict[str, str]]] = {
            "user_agent": browser_config['useragent']
        }

        if browser_config['sec_ch_ua'] and browser_config['sec_ch_ua'].strip():
            context_options['extra_http_headers'] = {
                'sec-ch-ua': browser_config['sec_ch_ua']
            }

        return context_options

    async def _wait_for_signup_ready(self, page, context, index: int):
        click_count = 0
        signup_state = {}
        turnstile_state = {}
        cookies = []

        for attempt in range(18):
            signup_state = await self._read_signup_state(page)
            turnstile_state = await self._read_turnstile_state(page)
            cookies = await context.cookies()
            has_clearance = any(
                cookie.get("name") == "cf_clearance" and str(cookie.get("value") or "").strip()
                for cookie in cookies
            )
            challenge_present = bool(signup_state.get("challengePresent")) or bool(turnstile_state.get("hasWidget"))

            if signup_state.get("signUpButtonCount", 0) and not signup_state.get("emailInputCount", 0):
                try:
                    await page.locator("button:has-text('Sign up with email')").first.click(timeout=3000)
                    await asyncio.sleep(1)
                    signup_state = await self._read_signup_state(page)
                except Exception as exc:
                    if self.debug:
                        logger.debug(f"Browser {index}: Sign up with email click skipped: {str(exc)}")

            if signup_state.get("signupReady") or has_clearance:
                break

            if challenge_present and attempt in {1, 3, 5, 8, 11, 14}:
                click_success = await self._click_turnstile_box(page, index)
                if not click_success:
                    click_success = await self._try_click_strategies(page, index)
                click_count += 1
                logger.info(f"Browser {index}: signup ready click attempt {click_count} success={click_success}")
                await asyncio.sleep(2)
                continue

            if attempt in {0, 4, 9, 14, 17}:
                logger.info(
                    f"Browser {index}: wait signup ready attempt={attempt + 1} "
                    f"ready={bool(signup_state.get('signupReady'))} "
                    f"challenge={challenge_present} "
                    f"cookies={','.join(sorted(str(cookie.get('name') or '') for cookie in cookies if cookie.get('name'))) or '-'} "
                    f"title={str(signup_state.get('title') or '-')}"
                )

            await asyncio.sleep(1.2)

        signup_state = await self._read_signup_state(page)
        turnstile_state = await self._read_turnstile_state(page)
        cookies = await context.cookies()
        return signup_state, turnstile_state, cookies

    async def _submit_email_address(self, page, index: int, email: str) -> bool:
        try:
            sign_up_button_count = 0
            email_input_count = 0
            try:
                sign_up_button_count = await page.locator("button:has-text('Sign up with email')").count()
            except Exception:
                sign_up_button_count = 0
            try:
                email_input_count = await page.locator('input[name="email"]').count()
            except Exception:
                email_input_count = 0

            if sign_up_button_count > 0 and email_input_count == 0:
                try:
                    await page.locator("button:has-text('Sign up with email')").first.click(timeout=5000)
                    await asyncio.sleep(1)
                except Exception as exc:
                    if self.debug:
                        logger.debug(f"Browser {index}: Sign up with email button click failed: {str(exc)}")

            email_input = page.locator('input[name="email"]').first
            await email_input.wait_for(timeout=10000)
            await email_input.click()
            try:
                await email_input.fill("")
            except Exception:
                pass
            await email_input.type(email, delay=random.randint(30, 70))
            await asyncio.sleep(0.5)
            await email_input.press("Enter")
            logger.info(f"Browser {index}: Submitted email address {email}")
            await asyncio.sleep(2)
            return True
        except Exception as exc:
            logger.warning(f"Browser {index}: submit email address failed: {str(exc)}")
            return False

    async def _fallback_submit_email(self, page, index: int) -> bool:
        try:
            await page.evaluate("""() => {
                const email = document.querySelector('input[name="email"]');
                if (!email) return false;
                const form = email.form;
                if (!form) return false;
                if (typeof form.requestSubmit === 'function') {
                    form.requestSubmit();
                } else {
                    form.submit();
                }
                return true;
            }""")
            logger.info(f"Browser {index}: Executed fallback email form submit")
            return True
        except Exception as exc:
            logger.warning(f"Browser {index}: fallback email form submit failed: {str(exc)}")
            return False

    async def _read_verify_email(self, page) -> str:
        try:
            result = await page.evaluate("""() => {
                const text = document.body ? document.body.innerText : '';
                const match = text.match(/We(?:'|’)ve emailed .*? to\\s+([^\\s]+@[^\\s]+)\\.?/i);
                return match ? match[1] : '';
            }""")
            return str(result or "").strip().lower().rstrip('.')
        except Exception:
            return ""

    async def _detect_rejected_email_domain(self, page) -> str:
        try:
            result = await page.evaluate("""() => {
                const text = document.body ? document.body.innerText : '';
                const match = text.match(/Your email domain\\s+([^\\s]+)\\s+has been rejected\\./i);
                return match ? match[1] : '';
            }""")
            return str(result or "").strip().lower()
        except Exception:
            return ""

    async def _wait_for_email_verify_step(self, page, index: int, email: str):
        expected_email = (email or "").strip().lower()
        last_displayed_email = ""

        for attempt in range(18):
            displayed_email = await self._read_verify_email(page)
            if displayed_email:
                last_displayed_email = displayed_email

            rejected_domain = await self._detect_rejected_email_domain(page)
            if rejected_domain:
                return {
                    "verifyStepReady": False,
                    "displayedEmail": last_displayed_email,
                    "domainRejected": rejected_domain,
                }

            try:
                password_count = await page.locator('input[name="password"]').count()
            except Exception:
                password_count = 0

            try:
                code_input_count = await page.locator(
                    "input[autocomplete='one-time-code'], "
                    "input[inputmode='numeric'], "
                    "input[maxlength='1'], "
                    "input[name*='code'], "
                    "input[id*='code']"
                ).count()
            except Exception:
                code_input_count = 0

            try:
                email_input_visible = await page.locator('input[name="email"]:visible').count() > 0
            except Exception:
                email_input_visible = False

            verify_ready = (
                code_input_count > 0
                or password_count > 0
                or (displayed_email and displayed_email == expected_email)
            )
            if verify_ready:
                logger.info(
                    f"Browser {index}: verify step ready displayed_email={displayed_email or '-'} "
                    f"code_inputs={code_input_count} password_inputs={password_count}"
                )
                return {
                    "verifyStepReady": True,
                    "displayedEmail": displayed_email or last_displayed_email,
                    "domainRejected": "",
                }

            if email_input_visible and attempt in {2, 5, 9}:
                await self._fallback_submit_email(page, index)
                await asyncio.sleep(2)
                continue

            if attempt in {4, 9, 14, 17}:
                logger.info(
                    f"Browser {index}: waiting verify step attempt={attempt + 1} "
                    f"displayed_email={displayed_email or '-'} "
                    f"email_input_visible={email_input_visible} code_inputs={code_input_count} "
                    f"password_inputs={password_count}"
                )

            await asyncio.sleep(1)

        return {
            "verifyStepReady": False,
            "displayedEmail": last_displayed_email,
            "domainRejected": "",
        }

    async def _get_turnstile_box(self, page, index: int):
        """获取 Cloudflare iframe 的坐标。"""
        for frame in page.frames:
            frame_url = frame.url or ""
            if "challenges.cloudflare.com" not in frame_url and "turnstile" not in frame_url:
                continue
            try:
                frame_el = await frame.frame_element()
                box = await frame_el.bounding_box()
                if box and box.get("width", 0) > 0 and box.get("height", 0) > 0:
                    return box
            except Exception as exc:
                if self.debug:
                    logger.debug(f"Browser {index}: Failed to get iframe box: {str(exc)}")
        return None

    async def _click_turnstile_box(self, page, index: int):
        """模拟旧脚本的鼠标点击方式点击 Turnstile iframe。"""
        box = await self._get_turnstile_box(page, index)
        if not box:
            return False

        try:
            jitter_x = random.uniform(-2, 4)
            jitter_y = random.uniform(-3, 3)
            click_x = box["x"] + min(28, max(box["width"] * 0.3, 18)) + jitter_x
            click_y = box["y"] + box["height"] / 2 + jitter_y

            mid_x = click_x + random.uniform(30, 90)
            mid_y = click_y + random.uniform(-35, 35)
            await page.mouse.move(mid_x, mid_y)
            await asyncio.sleep(random.uniform(0.08, 0.2))
            await page.mouse.move(click_x, click_y)
            await asyncio.sleep(random.uniform(0.03, 0.1))
            await page.mouse.click(click_x, click_y)

            if self.debug:
                logger.debug(
                    f"Browser {index}: Clicked turnstile iframe at ({click_x:.1f}, {click_y:.1f})"
                )
            return True
        except Exception as exc:
            if self.debug:
                logger.debug(f"Browser {index}: Mouse click on turnstile iframe failed: {str(exc)}")
            return False

    async def _inject_captcha_directly(self, page, websiteKey: str, action: str = '', cdata: str = '', index: int = 0):
        """Inject CAPTCHA directly into the target website"""
        script = f"""
        // 只清理之前注入的节点，避免破坏站点已有的 Turnstile
        document.querySelectorAll('[data-turnstile-injected="1"]').forEach(el => el.remove());

        const syncToken = (token) => {{
            if (!token) return;
            window.__turnstile_token = token;
            window.__cf_turnstile_token = token;
            window.__cfTurnstileToken = token;
            window.__last_turnstile_token = token;

            const ensureField = (tagName) => {{
                let node = document.querySelector(`${{tagName}}[name="cf-turnstile-response"]`);
                if (!node) {{
                    node = document.createElement(tagName);
                    node.name = 'cf-turnstile-response';
                    if (tagName === 'input') {{
                        node.type = 'hidden';
                    }}
                    document.body.appendChild(node);
                }}
                node.value = token;
                node.setAttribute('value', token);
                node.dispatchEvent(new Event('input', {{ bubbles: true }}));
                node.dispatchEvent(new Event('change', {{ bubbles: true }}));
            }};

            ensureField('input');
            ensureField('textarea');
        }};
        
        // Create turnstile widget directly on the page
        const captchaDiv = document.createElement('div');
        captchaDiv.className = 'cf-turnstile';
        captchaDiv.setAttribute('data-turnstile-injected', '1');
        captchaDiv.setAttribute('data-sitekey', '{websiteKey}');
        captchaDiv.setAttribute('data-callback', 'onTurnstileCallback');
        {f'captchaDiv.setAttribute("data-action", "{action}");' if action else ''}
        {f'captchaDiv.setAttribute("data-cdata", "{cdata}");' if cdata else ''}
        captchaDiv.style.position = 'fixed';
        captchaDiv.style.top = '20px';
        captchaDiv.style.left = '20px';
        captchaDiv.style.zIndex = '9999';
        captchaDiv.style.backgroundColor = 'white';
        captchaDiv.style.padding = '15px';
        captchaDiv.style.border = '2px solid #0f79af';
        captchaDiv.style.borderRadius = '8px';
        captchaDiv.style.boxShadow = '0 4px 12px rgba(0, 0, 0, 0.3)';
        
        // Add to body immediately
        document.body.appendChild(captchaDiv);
        
        // Load Turnstile script and render widget
        const loadTurnstile = () => {{
            const script = document.createElement('script');
            script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js';
            script.async = true;
            script.defer = true;
            script.onload = function() {{
                console.log('Turnstile script loaded');
                // Wait a bit for script to initialize
                setTimeout(() => {{
                    if (window.turnstile && window.turnstile.render) {{
                        try {{
                            window.turnstile.render(captchaDiv, {{
                                sitekey: '{websiteKey}',
                                {f'action: "{action}",' if action else ''}
                                {f'cdata: "{cdata}",' if cdata else ''}
                                callback: function(token) {{
                                    console.log('Turnstile solved with token:', token);
                                    syncToken(token);
                                }},
                                'error-callback': function(error) {{
                                    console.log('Turnstile error:', error);
                                    window.__turnstile_last_error = String(error || 'render-error');
                                }}
                            }});
                        }} catch (e) {{
                            console.log('Turnstile render error:', e);
                            window.__turnstile_last_error = String(e || 'render-exception');
                        }}
                    }} else {{
                        console.log('Turnstile API not available');
                        window.__turnstile_last_error = 'turnstile-api-unavailable';
                    }}
                }}, 1000);
            }};
            script.onerror = function() {{
                console.log('Failed to load Turnstile script');
                window.__turnstile_last_error = 'turnstile-script-load-failed';
            }};
            document.head.appendChild(script);
        }};
        
        // Check if Turnstile is already loaded
        if (window.turnstile) {{
            console.log('Turnstile already loaded, rendering immediately');
            try {{
                window.turnstile.render(captchaDiv, {{
                    sitekey: '{websiteKey}',
                    {f'action: "{action}",' if action else ''}
                    {f'cdata: "{cdata}",' if cdata else ''}
                    callback: function(token) {{
                        console.log('Turnstile solved with token:', token);
                        syncToken(token);
                    }},
                    'error-callback': function(error) {{
                        console.log('Turnstile error:', error);
                        window.__turnstile_last_error = String(error || 'render-error');
                    }}
                }});
            }} catch (e) {{
                console.log('Immediate render error:', e);
                window.__turnstile_last_error = String(e || 'render-exception');
                loadTurnstile();
            }}
        }} else {{
            loadTurnstile();
        }}
        
        // Setup global callback
        window.onTurnstileCallback = function(token) {{
            syncToken(token);
            console.log('Global turnstile callback executed:', token);
        }};
        """

        await page.evaluate(script)
        if self.debug:
            logger.debug(f"Browser {index}: Injected CAPTCHA directly into website with sitekey: {websiteKey}")

    async def _solve_turnstile(self, task_id: str, url: str, sitekey: str, action: Optional[str] = None, cdata: Optional[str] = None):
        """Solve the Turnstile challenge."""
        proxy = None

        index, browser, browser_config = await self.browser_pool.get()
        
        try:
            if hasattr(browser, 'is_connected') and not browser.is_connected():
                if self.debug:
                    logger.warning(f"Browser {index}: Browser disconnected, skipping")
                await self.browser_pool.put((index, browser, browser_config))
                await save_result(task_id, "turnstile", {"value": "CAPTCHA_FAIL", "elapsed_time": 0})
                return
        except Exception as e:
            if self.debug:
                logger.warning(f"Browser {index}: Cannot check browser state: {str(e)}")

        if self.proxy_support:
            proxy_file_path = os.path.join(os.getcwd(), "proxies.txt")

            try:
                with open(proxy_file_path) as proxy_file:
                    proxies = [line.strip() for line in proxy_file if line.strip()]

                proxy = random.choice(proxies) if proxies else None
                
                if self.debug and proxy:
                    logger.debug(f"Browser {index}: Selected proxy: {proxy}")
                elif self.debug and not proxy:
                    logger.debug(f"Browser {index}: No proxies available")
                    
            except FileNotFoundError:
                logger.warning(f"Proxy file not found: {proxy_file_path}")
                proxy = None
            except Exception as e:
                logger.error(f"Error reading proxy file: {str(e)}")
                proxy = None

            if proxy:
                if '@' in proxy:
                    try:
                        scheme_part, auth_part = proxy.split('://')
                        auth, address = auth_part.split('@')
                        username, password = auth.split(':')
                        ip, port = address.split(':')
                        if self.debug:
                            logger.debug(f"Browser {index}: Creating context with proxy {scheme_part}://{ip}:{port} (auth: {username}:***)")
                        context_options = {
                            "proxy": {
                                "server": f"{scheme_part}://{ip}:{port}",
                                "username": username,
                                "password": password
                            },
                            "user_agent": browser_config['useragent']
                        }
                        
                        if browser_config['sec_ch_ua'] and browser_config['sec_ch_ua'].strip():
                            context_options['extra_http_headers'] = {
                                'sec-ch-ua': browser_config['sec_ch_ua']
                            }
                        
                        context = await browser.new_context(**context_options)
                    except ValueError:
                        raise ValueError(f"Invalid proxy format: {proxy}")
                else:
                    parts = proxy.split(':')
                    if len(parts) == 5:
                        proxy_scheme, proxy_ip, proxy_port, proxy_user, proxy_pass = parts
                        if self.debug:
                            logger.debug(f"Browser {index}: Creating context with proxy {proxy_scheme}://{proxy_ip}:{proxy_port} (auth: {proxy_user}:***)")
                        context_options = {
                            "proxy": {
                                "server": f"{proxy_scheme}://{proxy_ip}:{proxy_port}",
                                "username": proxy_user,
                                "password": proxy_pass
                            },
                            "user_agent": browser_config['useragent']
                        }
                        
                        if browser_config['sec_ch_ua'] and browser_config['sec_ch_ua'].strip():
                            context_options['extra_http_headers'] = {
                                'sec-ch-ua': browser_config['sec_ch_ua']
                            }
                        
                        context = await browser.new_context(**context_options)
                    elif len(parts) == 3:
                        if self.debug:
                            logger.debug(f"Browser {index}: Creating context with proxy {proxy}")
                        context_options = {
                            "proxy": {"server": f"{proxy}"},
                            "user_agent": browser_config['useragent']
                        }
                        
                        if browser_config['sec_ch_ua'] and browser_config['sec_ch_ua'].strip():
                            context_options['extra_http_headers'] = {
                                'sec-ch-ua': browser_config['sec_ch_ua']
                            }
                        
                        context = await browser.new_context(**context_options)
                    else:
                        raise ValueError(f"Invalid proxy format: {proxy}")
            else:
                if self.debug:
                    logger.debug(f"Browser {index}: Creating context without proxy")
                context_options = {"user_agent": browser_config['useragent']}
                
                if browser_config['sec_ch_ua'] and browser_config['sec_ch_ua'].strip():
                    context_options['extra_http_headers'] = {
                        'sec-ch-ua': browser_config['sec_ch_ua']
                    }
                
                context = await browser.new_context(**context_options)
        else:
            context_options = {"user_agent": browser_config['useragent']}
            
            if browser_config['sec_ch_ua'] and browser_config['sec_ch_ua'].strip():
                context_options['extra_http_headers'] = {
                    'sec-ch-ua': browser_config['sec_ch_ua']
                }
            
            context = await browser.new_context(**context_options)

        page = await context.new_page()
        
        await self._antishadow_inject(page)
        
        await self._block_rendering(page)
        
        await page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined,
        });
        
        window.chrome = {
            runtime: {},
            loadTimes: function() {},
            csi: function() {},
        };
        """)
        
        if self.browser_type in ['chromium', 'chrome', 'msedge']:
            await page.set_viewport_size({"width": 1280, "height": 900})
            if self.debug:
                logger.debug(f"Browser {index}: Set viewport size to 1280x900")

        start_time = time.time()

        try:
            if self.debug:
                logger.debug(f"Browser {index}: Starting Turnstile solve for URL: {url} with Sitekey: {sitekey} | Action: {action} | Cdata: {cdata} | Proxy: {proxy}")
                logger.debug(f"Browser {index}: Setting up optimized page loading with resource blocking")

            if self.debug:
                logger.debug(f"Browser {index}: Loading real website directly: {url}")

            await page.goto(url, wait_until='domcontentloaded', timeout=30000)

            await self._unblock_rendering(page)

            try:
                await page.wait_for_load_state('networkidle', timeout=10000)
            except Exception:
                if self.debug:
                    logger.debug(f"Browser {index}: networkidle wait timed out, continuing")

            await asyncio.sleep(2)

            widget_source = "page"
            elements = []
            for _ in range(3):
                elements = await self._find_turnstile_elements(page, index)
                if elements:
                    break
                await asyncio.sleep(1)

            if not elements:
                widget_source = "injected"
                if self.debug:
                    logger.debug(f"Browser {index}: No page widget found, injecting fallback widget")
                await self._inject_captcha_directly(page, sitekey, action or '', cdata or '', index)
                await asyncio.sleep(2)
                elements = await self._find_turnstile_elements(page, index)
            elif self.debug:
                logger.debug(f"Browser {index}: Using existing page widget: {elements}")

            max_attempts = 45
            click_count = 0
            max_clicks = 12
            failure_reason = ""

            for attempt in range(max_attempts):
                try:
                    token = await self._read_turnstile_token(page)
                    if token:
                        elapsed_time = round(time.time() - start_time, 3)
                        logger.success(f"Browser {index}: Successfully solved captcha - {COLORS.get('MAGENTA')}{token[:10]}{COLORS.get('RESET')} in {COLORS.get('GREEN')}{elapsed_time}{COLORS.get('RESET')} Seconds")
                        await save_result(task_id, "turnstile", {"value": token, "elapsed_time": elapsed_time})
                        return

                    state = await self._read_turnstile_state(page)
                    failure_reason = state.get("lastError") or ""

                    if attempt > 1 and attempt % 3 == 0 and click_count < max_clicks:
                        click_success = await self._click_turnstile_box(page, index)
                        if not click_success:
                            click_success = await self._try_click_strategies(page, index)
                        click_count += 1
                        if click_success and self.debug:
                            logger.debug(f"Browser {index}: Click successful (click #{click_count}/{max_clicks})")
                        elif not click_success and self.debug:
                            logger.debug(f"Browser {index}: All click strategies failed on attempt {attempt + 1} (click #{click_count}/{max_clicks})")

                    # Адаптивное ожидание
                    wait_time = min(0.5 + (attempt * 0.05), 2.0)
                    await asyncio.sleep(wait_time)

                    if self.debug and attempt % 5 == 0:
                        logger.debug(
                            f"Browser {index}: Attempt {attempt + 1}/{max_attempts} - Waiting for token "
                            f"(source: {widget_source}, widgets: {state.get('hasWidget')}, "
                            f"iframes: {state.get('iframeCount')}, responses: {state.get('responseCount')}, "
                            f"filled: {state.get('filledCount')}, clicks: {click_count}/{max_clicks}, "
                            f"last_error: {failure_reason or '-'} )"
                        )

                except Exception as e:
                    if self.debug:
                        logger.debug(f"Browser {index}: Attempt {attempt + 1} error: {str(e)}")
                    continue
            
            elapsed_time = round(time.time() - start_time, 3)
            state = {}
            try:
                state = await self._read_turnstile_state(page)
            except Exception:
                state = {}
            failure_reason = (
                failure_reason
                or state.get("lastError")
                or ("widget-not-found" if not state.get("hasWidget") else "")
                or "timeout-no-token"
            )
            await save_result(
                task_id,
                "turnstile",
                {"value": "CAPTCHA_FAIL", "elapsed_time": elapsed_time, "reason": failure_reason},
            )
            if self.debug:
                logger.error(
                    f"Browser {index}: Error solving Turnstile in {COLORS.get('RED')}{elapsed_time}{COLORS.get('RESET')} Seconds "
                    f"(reason: {failure_reason})"
                )
        except Exception as e:
            elapsed_time = round(time.time() - start_time, 3)
            await save_result(
                task_id,
                "turnstile",
                {"value": "CAPTCHA_FAIL", "elapsed_time": elapsed_time, "reason": str(e)},
            )
            if self.debug:
                logger.error(f"Browser {index}: Error solving Turnstile: {str(e)}")
        finally:
            if self.debug:
                logger.debug(f"Browser {index}: Closing browser context and cleaning up")
            
            try:
                await context.close()
                if self.debug:
                    logger.debug(f"Browser {index}: Context closed successfully")
            except Exception as e:
                if self.debug:
                    logger.warning(f"Browser {index}: Error closing context: {str(e)}")
            
            try:
                if hasattr(browser, 'is_connected') and browser.is_connected():
                    await self.browser_pool.put((index, browser, browser_config))
                    if self.debug:
                        logger.debug(f"Browser {index}: Browser returned to pool")
                else:
                    if self.debug:
                        logger.warning(f"Browser {index}: Browser disconnected, not returning to pool")
            except Exception as e:
                if self.debug:
                    logger.warning(f"Browser {index}: Error returning browser to pool: {str(e)}")






    async def _preflight_signup(self, url: str):
        index, browser, browser_config = await self.browser_pool.get()
        context = None
        page = None

        try:
            try:
                if hasattr(browser, 'is_connected') and not browser.is_connected():
                    logger.warning(f"Browser {index}: Browser disconnected during preflight")
                    return {
                        "signupReady": False,
                        "challengePresent": False,
                        "error": "browser disconnected",
                        "title": "",
                        "url": url,
                        "bodySnippet": "",
                        "cookies": [],
                        "userAgent": browser_config.get("useragent") or "",
                    }
            except Exception as exc:
                if self.debug:
                    logger.warning(f"Browser {index}: Cannot check browser state during preflight: {str(exc)}")

            context = await browser.new_context(**self._build_context_options(browser_config))
            page = await context.new_page()

            await self._antishadow_inject(page)
            await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined,
            });

            window.chrome = {
                runtime: {},
                loadTimes: function() {},
                csi: function() {},
            };
            """)

            if self.browser_type in ['chromium', 'chrome', 'msedge']:
                await page.set_viewport_size({"width": 1280, "height": 900})

            logger.info(f"Browser {index}: Starting signup preflight for {url}")
            await page.goto(url, wait_until='domcontentloaded', timeout=45000)

            try:
                await page.wait_for_load_state('networkidle', timeout=5000)
            except Exception:
                if self.debug:
                    logger.debug(f"Browser {index}: signup preflight networkidle wait timed out")

            signup_state, turnstile_state, cookies = await self._wait_for_signup_ready(page, context, index)
            has_clearance = any(
                cookie.get("name") == "cf_clearance" and str(cookie.get("value") or "").strip()
                for cookie in cookies
            )
            challenge_present = bool(signup_state.get("challengePresent")) or bool(turnstile_state.get("hasWidget"))

            payload = {
                "signupReady": bool(signup_state.get("signupReady")),
                "challengePresent": challenge_present,
                "title": str(signup_state.get("title") or ""),
                "url": str(signup_state.get("url") or url),
                "bodySnippet": str(signup_state.get("bodySnippet") or ""),
                "emailInputCount": int(signup_state.get("emailInputCount") or 0),
                "signUpButtonCount": int(signup_state.get("signUpButtonCount") or 0),
                "passwordInputCount": int(signup_state.get("passwordInputCount") or 0),
                "hasCfClearance": has_clearance,
                "turnstileState": turnstile_state,
                "cookies": cookies,
                "userAgent": browser_config.get("useragent") or "",
            }
            logger.info(
                f"Browser {index}: signup preflight done "
                f"ready={payload['signupReady']} challenge={payload['challengePresent']} "
                f"has_cf_clearance={payload['hasCfClearance']} "
                f"cookies={','.join(sorted(str(cookie.get('name') or '') for cookie in cookies if cookie.get('name'))) or '-'} "
                f"title={payload['title'] or '-'} body={str(payload['bodySnippet'])[:180] or '-'}"
            )
            return payload
        except Exception as exc:
            logger.warning(f"Browser {index}: signup preflight failed: {str(exc)}")
            return {
                "signupReady": False,
                "challengePresent": False,
                "error": str(exc),
                "title": "",
                "url": url,
                "bodySnippet": "",
                "cookies": [],
                "userAgent": browser_config.get("useragent") or "",
            }
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass

            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass

            try:
                await self.browser_pool.put((index, browser, browser_config))
            except Exception as exc:
                if self.debug:
                    logger.warning(f"Browser {index}: Error returning browser after preflight: {str(exc)}")

    async def preflight_signup(self):
        """预热 signup 页面并返回 Cloudflare/页面诊断信息。"""
        url = request.args.get('url')
        if not url:
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_WRONG_PAGEURL",
                "errorDescription": "Missing 'url' parameter",
            }), 400

        payload = await self._preflight_signup(url)
        return jsonify(payload), 200

    async def _signup_email_via_browser(self, url: str, email: str):
        index, browser, browser_config = await self.browser_pool.get()
        context = None
        page = None

        try:
            try:
                if hasattr(browser, 'is_connected') and not browser.is_connected():
                    logger.warning(f"Browser {index}: Browser disconnected during signup-email")
                    return {
                        "submitted": False,
                        "verifyStepReady": False,
                        "displayedEmail": "",
                        "domainRejected": "",
                        "title": "",
                        "url": url,
                        "bodySnippet": "",
                        "cookies": [],
                        "userAgent": browser_config.get("useragent") or "",
                        "error": "browser disconnected",
                    }
            except Exception as exc:
                if self.debug:
                    logger.warning(f"Browser {index}: Cannot check browser state during signup-email: {str(exc)}")

            context = await browser.new_context(**self._build_context_options(browser_config))
            page = await context.new_page()

            await self._antishadow_inject(page)
            await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined,
            });

            window.chrome = {
                runtime: {},
                loadTimes: function() {},
                csi: function() {},
            };
            """)

            if self.browser_type in ['chromium', 'chrome', 'msedge']:
                await page.set_viewport_size({"width": 1280, "height": 900})

            logger.info(f"Browser {index}: Starting signup-email for {email} on {url}")
            await page.goto(url, wait_until='domcontentloaded', timeout=45000)

            try:
                await page.wait_for_load_state('networkidle', timeout=5000)
            except Exception:
                if self.debug:
                    logger.debug(f"Browser {index}: signup-email networkidle wait timed out")

            await self._wait_for_signup_ready(page, context, index)
            submitted = await self._submit_email_address(page, index, email)
            verify_result = await self._wait_for_email_verify_step(page, index, email)

            signup_state = await self._read_signup_state(page)
            turnstile_state = await self._read_turnstile_state(page)
            cookies = await context.cookies()
            has_clearance = any(
                cookie.get("name") == "cf_clearance" and str(cookie.get("value") or "").strip()
                for cookie in cookies
            )

            payload = {
                "submitted": submitted,
                "verifyStepReady": bool(verify_result.get("verifyStepReady")),
                "displayedEmail": str(verify_result.get("displayedEmail") or ""),
                "domainRejected": str(verify_result.get("domainRejected") or ""),
                "challengePresent": bool(signup_state.get("challengePresent")) or bool(turnstile_state.get("hasWidget")),
                "title": str(signup_state.get("title") or ""),
                "url": str(signup_state.get("url") or url),
                "bodySnippet": str(signup_state.get("bodySnippet") or ""),
                "hasCfClearance": has_clearance,
                "turnstileState": turnstile_state,
                "cookies": cookies,
                "userAgent": browser_config.get("useragent") or "",
            }
            logger.info(
                f"Browser {index}: signup-email done submitted={payload['submitted']} "
                f"verify_ready={payload['verifyStepReady']} displayed_email={payload['displayedEmail'] or '-'} "
                f"domain_rejected={payload['domainRejected'] or '-'} has_cf_clearance={payload['hasCfClearance']} "
                f"title={payload['title'] or '-'} body={str(payload['bodySnippet'])[:180] or '-'}"
            )
            return payload
        except Exception as exc:
            logger.warning(f"Browser {index}: signup-email failed: {str(exc)}")
            return {
                "submitted": False,
                "verifyStepReady": False,
                "displayedEmail": "",
                "domainRejected": "",
                "title": "",
                "url": url,
                "bodySnippet": "",
                "cookies": [],
                "userAgent": browser_config.get("useragent") or "",
                "error": str(exc),
            }
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass

            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass

            try:
                await self.browser_pool.put((index, browser, browser_config))
            except Exception as exc:
                if self.debug:
                    logger.warning(f"Browser {index}: Error returning browser after signup-email: {str(exc)}")

    async def signup_email(self):
        url = request.args.get('url')
        email = request.args.get('email')
        if not url or not email:
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_WRONG_PARAMS",
                "errorDescription": "Both 'url' and 'email' are required",
            }), 400

        payload = await self._signup_email_via_browser(url, email)
        return jsonify(payload), 200

    async def process_turnstile(self):
        """Handle the /turnstile endpoint requests."""
        url = request.args.get('url')
        sitekey = request.args.get('sitekey')
        action = request.args.get('action')
        cdata = request.args.get('cdata')

        if not url or not sitekey:
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_WRONG_PAGEURL",
                "errorDescription": "Both 'url' and 'sitekey' are required"
            }), 200

        task_id = str(uuid.uuid4())
        await save_result(task_id, "turnstile", {
            "status": "CAPTCHA_NOT_READY",
            "createTime": int(time.time()),
            "url": url,
            "sitekey": sitekey,
            "action": action,
            "cdata": cdata
        })

        try:
            asyncio.create_task(self._solve_turnstile(task_id=task_id, url=url, sitekey=sitekey, action=action, cdata=cdata))

            if self.debug:
                logger.debug(f"Request completed with taskid {task_id}.")
            return jsonify({
                "errorId": 0,
                "taskId": task_id
            }), 200
        except Exception as e:
            logger.error(f"Unexpected error processing request: {str(e)}")
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_UNKNOWN",
                "errorDescription": str(e)
            }), 200

    async def get_result(self):
        """Return solved data"""
        task_id = request.args.get('id')

        if not task_id:
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_WRONG_CAPTCHA_ID",
                "errorDescription": "Invalid task ID/Request parameter"
            }), 200

        result = await load_result(task_id)
        if not result:
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_CAPTCHA_UNSOLVABLE",
                "errorDescription": "Task not found"
            }), 200

        if result == "CAPTCHA_NOT_READY" or (isinstance(result, dict) and result.get("status") == "CAPTCHA_NOT_READY"):
            return jsonify({"status": "processing"}), 200

        if isinstance(result, dict) and result.get("value") == "CAPTCHA_FAIL":
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_CAPTCHA_UNSOLVABLE",
                "errorDescription": result.get("reason") or "Workers could not solve the Captcha"
            }), 200

        if isinstance(result, dict) and result.get("value") and result.get("value") != "CAPTCHA_FAIL":
            return jsonify({
                "errorId": 0,
                "status": "ready",
                "solution": {
                    "token": result["value"]
                }
            }), 200
        else:
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_CAPTCHA_UNSOLVABLE",
                "errorDescription": "Workers could not solve the Captcha"
            }), 200

    

    @staticmethod
    async def index():
        """Serve the API documentation page."""
        return """
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Turnstile Solver API</title>
                <script src="https://cdn.tailwindcss.com"></script>
            </head>
            <body class="bg-gray-900 text-gray-200 min-h-screen flex items-center justify-center">
                <div class="bg-gray-800 p-8 rounded-lg shadow-md max-w-2xl w-full border border-red-500">
                    <h1 class="text-3xl font-bold mb-6 text-center text-red-500">Welcome to Turnstile Solver API</h1>

                    <p class="mb-4 text-gray-300">To use the turnstile service, send a GET request to 
                       <code class="bg-red-700 text-white px-2 py-1 rounded">/turnstile</code> with the following query parameters:</p>

                    <ul class="list-disc pl-6 mb-6 text-gray-300">
                        <li><strong>url</strong>: The URL where Turnstile is to be validated</li>
                        <li><strong>sitekey</strong>: The site key for Turnstile</li>
                    </ul>

                    <div class="bg-gray-700 p-4 rounded-lg mb-6 border border-red-500">
                        <p class="font-semibold mb-2 text-red-400">Example usage:</p>
                        <code class="text-sm break-all text-red-300">/turnstile?url=https://example.com&sitekey=sitekey</code>
                    </div>


                    <div class="bg-gray-700 p-4 rounded-lg mb-6">
                        <p class="text-gray-200 font-semibold mb-3">📢 Connect with Us</p>
                        <div class="space-y-2 text-sm">
                            <p class="text-gray-300">
                                📢 <strong>Channel:</strong> 
                                <a href="https://t.me/D3_vin" class="text-red-300 hover:underline">https://t.me/D3_vin</a> 
                                - Latest updates and releases
                            </p>
                            <p class="text-gray-300">
                                💬 <strong>Chat:</strong> 
                                <a href="https://t.me/D3vin_chat" class="text-red-300 hover:underline">https://t.me/D3vin_chat</a> 
                                - Community support and discussions
                            </p>
                            <p class="text-gray-300">
                                📁 <strong>GitHub:</strong> 
                                <a href="https://github.com/D3-vin" class="text-red-300 hover:underline">https://github.com/D3-vin</a> 
                                - Source code and development
                            </p>
                        </div>
                    </div>
                </div>
            </body>
            </html>
        """


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Turnstile API Server")

    parser.add_argument('--no-headless', action='store_true', help='Run the browser with GUI (disable headless mode). By default, headless mode is enabled.')
    parser.add_argument('--useragent', type=str, help='User-Agent string (if not specified, random configuration is used)')
    parser.add_argument('--debug', action='store_true', help='Enable or disable debug mode for additional logging and troubleshooting information (default: False)')
    parser.add_argument('--browser_type', type=str, default='chromium', help='Specify the browser type for the solver. Supported options: chromium, chrome, msedge, camoufox (default: chromium)')
    parser.add_argument('--thread', type=int, default=4, help='Set the number of browser threads to use for multi-threaded mode. Increasing this will speed up execution but requires more resources (default: 1)')
    parser.add_argument('--proxy', action='store_true', help='Enable proxy support for the solver (Default: False)')
    parser.add_argument('--random', action='store_true', help='Use random User-Agent and Sec-CH-UA configuration from pool')
    parser.add_argument('--browser', type=str, help='Specify browser name to use (e.g., chrome, firefox)')
    parser.add_argument('--version', type=str, help='Specify browser version to use (e.g., 139, 141)')
    parser.add_argument('--host', type=str, default='0.0.0.0', help='Specify the IP address where the API solver runs. (Default: 127.0.0.1)')
    parser.add_argument('--port', type=str, default='5072', help='Set the port for the API solver to listen on. (Default: 5072)')
    return parser.parse_args()


def create_app(headless: bool, useragent: str, debug: bool, browser_type: str, thread: int, proxy_support: bool, use_random_config: bool, browser_name: str, browser_version: str) -> Quart:
    server = TurnstileAPIServer(headless=headless, useragent=useragent, debug=debug, browser_type=browser_type, thread=thread, proxy_support=proxy_support, use_random_config=use_random_config, browser_name=browser_name, browser_version=browser_version)
    return server.app


if __name__ == '__main__':
    args = parse_args()
    browser_types = [
        'chromium',
        'chrome',
        'msedge',
        'camoufox',
    ]
    if args.browser_type not in browser_types:
        logger.error(f"Unknown browser type: {COLORS.get('RED')}{args.browser_type}{COLORS.get('RESET')} Available browser types: {browser_types}")
    else:
        app = create_app(
            headless=not args.no_headless, 
            debug=args.debug, 
            useragent=args.useragent, 
            browser_type=args.browser_type, 
            thread=args.thread, 
            proxy_support=args.proxy,
            use_random_config=args.random,
            browser_name=args.browser,
            browser_version=args.version
        )
        app.run(host=args.host, port=int(args.port))
