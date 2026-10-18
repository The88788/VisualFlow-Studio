// setup_puppeteer.js - ИСПРАВЛЕННАЯ ВЕРСИЯ
// Работает с новыми версиями Puppeteer (без waitForTimeout)

const puppeteer = require('puppeteer-extra');
const StealthPlugin = require('puppeteer-extra-plugin-stealth');
const fs = require('fs').promises;
const path = require('path');

// Настройка stealth plugin с явным включением всех evasions
const stealth = StealthPlugin();
// Убеждаемся что все evasion-модули включены
stealth.enabledEvasions.add('chrome.app');
stealth.enabledEvasions.add('chrome.csi');
stealth.enabledEvasions.add('chrome.runtime');
stealth.enabledEvasions.add('navigator.languages');
stealth.enabledEvasions.add('navigator.permissions');
stealth.enabledEvasions.add('navigator.plugins');
stealth.enabledEvasions.add('navigator.webdriver');
stealth.enabledEvasions.add('media.codecs');
stealth.enabledEvasions.add('sourceurl');
stealth.enabledEvasions.add('user-agent-override');
stealth.enabledEvasions.add('webgl.vendor');
stealth.enabledEvasions.add('window.outerdimensions');
stealth.enabledEvasions.add('iframe.contentWindow');
puppeteer.use(stealth);

// Функция ожидания (замена устаревшей waitForTimeout)
const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));

class DebugHelper {
    constructor(profilePath) {
        this.debugDir = path.join(profilePath, 'debug');
        this.stepCounter = 0;
    }

    async init() {
        await fs.mkdir(this.debugDir, { recursive: true });
    }

    async capture(page, stepName) {
        this.stepCounter++;
        const timestamp = new Date().toISOString().split('T')[1].replace(/:/g, '').slice(0, 6);
        const prefix = `${String(this.stepCounter).padStart(2, '0')}_${timestamp}_${stepName}`;

        try {
            const screenshotPath = path.join(this.debugDir, `${prefix}.png`);
            await page.screenshot({ path: screenshotPath, fullPage: true });
            console.log(`   📸 ${screenshotPath}`);

            const html = await page.content();
            const htmlPath = path.join(this.debugDir, `${prefix}.html`);
            await fs.writeFile(htmlPath, html, 'utf-8');
            console.log(`   📄 ${htmlPath}`);

            const urlPath = path.join(this.debugDir, `${prefix}_url.txt`);
            await fs.writeFile(urlPath, page.url(), 'utf-8');
        } catch (error) {
            console.log(`   ⚠️ Ошибка capture: ${error.message}`);
        }
    }
}

function checkProxyValid(proxyStr) {
    return new Promise((resolve) => {
        try {
            const url = new URL(proxyStr);
            const http = require('http');
            const options = {
                host: url.hostname,
                port: url.port,
                method: 'CONNECT',
                path: 'www.google.com:443',
                timeout: 10000 // 10 seconds
            };
            if (url.username && url.password) {
                options.headers = {
                    'Proxy-Authorization': 'Basic ' + Buffer.from(url.username + ':' + url.password).toString('base64')
                };
            }
            const req = http.request(options);
            req.on('connect', (res, socket, head) => {
                socket.destroy();
                resolve(res.statusCode === 200);
            });
            req.on('error', () => resolve(false));
            req.on('timeout', () => { req.destroy(); resolve(false); });
            req.end();
        } catch (e) {
            resolve(false);
        }
    });
}

async function getRandomWorkingProxy(excludeProxies = []) {
    const proxyPoolPaths = [
        '/opt/gemini-api/config/proxy_pool.txt',
        '/opt/geminiapi/config/proxy_pool.txt',
        path.join(__dirname, 'config/proxy_pool.txt'),
        path.join(__dirname, 'proxy_pool.txt'),
        'C:/opt/gemini-api/config/proxy_pool.txt',
        'C:/opt/geminiapi/config/proxy_pool.txt'
    ];

    let poolData = '';
    for (const p of proxyPoolPaths) {
        try {
            poolData = await fs.readFile(p, 'utf8');
            console.log(`✅ Loaded proxy pool from: ${p}`);
            break;
        } catch (e) {}
    }
    if (!poolData) return null;

    // ps1,http://199.181.239.41:50100,artemkuts,cxz5RcWCzC,true
    const proxies = poolData.split('\n')
        .map(line => line.trim())
        .filter(line => line.length > 0 && !line.startsWith('#'))
        .map(line => {
            const parts = line.split(',');
            if (parts.length >= 4) {
                // protocol://ip:port
                let base = parts[1];
                let user = parts[2];
                let pass = parts[3];
                // Make a standard proxy url: http://user:pass@ip:port
                try {
                    const u = new URL(base);
                    u.username = user;
                    u.password = pass;
                    return u.toString();
                } catch(e) { return null; }
            }
            return null;
        })
        .filter(p => p !== null && !excludeProxies.includes(p));

    if (proxies.length === 0) return null;

    // Shuffle and test
    for (let i = proxies.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [proxies[i], proxies[j]] = [proxies[j], proxies[i]];
    }

    console.log(`🔄 Checking ${proxies.length} proxies from pool...`);
    for (const p of proxies.slice(0, Math.min(10, proxies.length))) { // Check up to 10 random
        const safeP = p.replace(/:[^:@]+@/, ':***@');
        const works = await checkProxyValid(p);
        if (works) {
            console.log(`   ✅ Found working proxy: ${safeP}`);
            return p;
        } else {
            console.log(`   ❌ Proxy check failed: ${safeP}`);
        }
    }
    return null;
}

async function updateAccountProxyInFile(email, newProxyStr) {
    const possiblePaths = [
        path.resolve(__dirname, 'config/accounts.json'),
        path.resolve(__dirname, 'accounts.json'),
        'config/accounts.json',
        '/mnt/user-data/uploads/accounts.json',
        'accounts.json'
    ];
    for (const configPath of possiblePaths) {
        try {
            const data = await fs.readFile(configPath, 'utf-8');
            const accounts = JSON.parse(data);
            let updated = false;
            for (const acc of accounts) {
                if (acc.email === email) {
                    acc.proxy = newProxyStr;
                    updated = true;
                    break;
                }
            }
            if (updated) {
                await fs.writeFile(configPath, JSON.stringify(accounts, null, 4), 'utf8');
                console.log(`💾 ОБНОВЛЕН ПРОКСИ в accounts.json для ${email}`);
                return true;
            }
        } catch (e) {
            // ignore
        }
    }
    return false;
}

async function setupAccount(account) {
    console.log(`\n${'='.repeat(60)}`);
    console.log(`🔐 Настройка: ${account.name} (${account.email})`);
    
    // Prefer residential_proxy for auth (lower traffic cost, sticky IP); fallback to datacenter proxy
    let currentProxyStr = account.residential_proxy || account.proxy;
    let proxyWorks = false;

    if (currentProxyStr) {
        const isResidential = !!account.residential_proxy;
        const safeProxy = currentProxyStr.replace(/:[^:@]+@/, ':***@');
        if (isResidential) {
            // Skip HTTP CONNECT check — residential proxies often block CONNECT to google.com
            // but work fine for actual browser traffic. Trust and proceed.
            proxyWorks = true;
            console.log(`🌐 Резидентский прокси (проверка пропущена): ${safeProxy}`);
        } else {
            console.log(`🌐 Проверка текущего прокси: ${safeProxy}`);
            proxyWorks = await checkProxyValid(currentProxyStr);
            if (!proxyWorks) {
                console.log('❌ Текущий прокси НЕ работает (или недоступен).');
            } else {
                console.log('✅ Текущий прокси доступен!');
            }
        }
    } else {
        console.log('⚠️ Аккаунт не имеет прокси.');
    }

    if (!proxyWorks) {
        console.log('🔄 Ищем новый рабочий прокси из proxy_pool.txt...');
        const exclude = currentProxyStr ? [currentProxyStr] : [];
        const newProxyStr = await getRandomWorkingProxy(exclude);
        if (newProxyStr) {
            account.proxy = newProxyStr;
            currentProxyStr = newProxyStr;
            await updateAccountProxyInFile(account.email, newProxyStr);
        } else {
            console.log('⚠️ Не удалось найти рабочий прокси! Продолжаем без/со старым прокси (на свой страх и риск)...');
        }
    }
    
    const proxyStr = currentProxyStr;
    if (proxyStr) {
        console.log(`🌐 Итоговый прокси: ${proxyStr.replace(/:[^:@]+@/, ':***@')}`);
    }
    console.log(`${'='.repeat(60)}\n`);

    const profilePath = account.profile_path + '_puppeteer';
    await fs.mkdir(profilePath, { recursive: true });
    console.log(`📁 Профиль: ${profilePath}\n`);

    const debug = new DebugHelper(profilePath);
    await debug.init();

    let proxyServer = null;
    let proxyUsername = null;
    let proxyPassword = null;

    if (proxyStr) {
        const proxyUrl = new URL(proxyStr);
        proxyServer = `${proxyUrl.hostname}:${proxyUrl.port}`;
        proxyUsername = proxyUrl.username;
        proxyPassword = proxyUrl.password;
    }

    let browser = null;
    let sessionToken = null;

    // Переменная для хранения spawned Chrome процесса
    let chromeProcess = null;

    try {
        console.log('🌐 Запуск браузера...');

        // Поиск реального Google Chrome (НЕ Chromium!)
        // Google блокирует вход из Chromium: "This browser or app may not be secure"
        const findChrome = () => {
            const { execSync } = require('child_process');
            const chromePaths = [
                // Linux
                '/usr/bin/google-chrome-stable',
                '/usr/bin/google-chrome',
                '/usr/local/bin/google-chrome',
                '/opt/google/chrome/google-chrome',
                // Mac
                '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
                // Windows
                'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
                'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
            ];

            for (const p of chromePaths) {
                try {
                    require('fs').accessSync(p);
                    console.log(`✅ Найден Google Chrome: ${p}`);
                    return p;
                } catch (e) { /* не найден */ }
            }

            // Попытка найти через which/where
            try {
                const result = execSync('which google-chrome-stable 2>/dev/null || which google-chrome 2>/dev/null', { encoding: 'utf-8' }).trim();
                if (result) {
                    console.log(`✅ Найден Google Chrome через which: ${result}`);
                    return result;
                }
            } catch (e) { /* не найден */ }

            return null;
        };

        const chromeExecutable = findChrome();
        if (!chromeExecutable) {
            console.log('❌ Google Chrome не найден! Установите: apt install -y google-chrome-stable');
            console.log('   Chromium НЕ подходит — Google блокирует вход из Chromium.');
            return null;
        }

        // КЛЮЧЕВОЙ МОМЕНТ: Запускаем Chrome ВРУЧНУЮ (без puppeteer.launch)
        // puppeteer.launch() добавляет --enable-automation, что Google детектит
        // и показывает "This browser or app may not be secure"
        
        const debugPort = 9222 + Math.floor(Math.random() * 1000);
        const chromeArgs = [
            `--remote-debugging-port=${debugPort}`,
            `--user-data-dir=${path.resolve(profilePath)}`,
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-dev-shm-usage',
            '--disable-gpu',
            '--disable-crash-reporter',
            '--disable-blink-features=AutomationControlled',
            '--window-size=1280,800',
            '--lang=en-US',
            '--accept-lang=en-US,en',
            '--disable-features=IsolateOrigins,site-per-process,AutomationControlled',
            '--disable-site-isolation-trials',
            '--enable-features=NetworkService,NetworkServiceInProcess',
            '--disable-background-timer-throttling',
            '--disable-backgrounding-occluded-windows',
            '--disable-renderer-backgrounding',
            '--disable-ipc-flooding-protection',
            '--flag-switches-begin',
            '--flag-switches-end',
            '--no-first-run',
            '--no-default-browser-check',
            '--disable-default-apps',
            '--disable-component-extensions-with-background-pages',
            // НЕ добавляем --enable-automation!
        ];

        if (proxyServer) {
            chromeArgs.push(`--proxy-server=${proxyServer}`);
        }

        // Стартовая страница
        chromeArgs.push('about:blank');

        console.log(`🔑 Запуск Chrome вручную (без --enable-automation): ${chromeExecutable}`);
        console.log(`   Debug port: ${debugPort}`);

        const { spawn } = require('child_process');
        chromeProcess = spawn(chromeExecutable, chromeArgs, {
            stdio: ['ignore', 'pipe', 'pipe'],
            detached: false,
        });

        chromeProcess.stderr.on('data', (data) => {
            const msg = data.toString();
            // Выводим только ошибки, не спам
            if (msg.includes('ERROR') || msg.includes('FATAL')) {
                console.log(`   Chrome stderr: ${msg.trim()}`);
            }
        });

        // Ждём пока Chrome поднимет DevTools порт
        console.log('   ⏳ Ждём запуск Chrome...');
        let browserWSEndpoint = null;
        for (let i = 0; i < 30; i++) {
            await sleep(1000);
            try {
                const http = require('http');
                const jsonUrl = `http://127.0.0.1:${debugPort}/json/version`;
                const data = await new Promise((resolve, reject) => {
                    http.get(jsonUrl, (res) => {
                        let body = '';
                        res.on('data', chunk => body += chunk);
                        res.on('end', () => resolve(JSON.parse(body)));
                    }).on('error', reject);
                });
                browserWSEndpoint = data.webSocketDebuggerUrl;
                console.log(`   ✅ Chrome запущен (попытка ${i + 1})`);
                break;
            } catch (e) {
                // Chrome ещё не готов
            }
        }

        if (!browserWSEndpoint) {
            throw new Error('Chrome не запустился за 30 секунд');
        }

        // Подключаемся через puppeteer.connect() — БЕЗ automation-флагов
        browser = await puppeteer.connect({
            browserWSEndpoint,
            defaultViewport: { width: 1280, height: 800 },
        });
        console.log('   ✅ Puppeteer подключён к Chrome');

        // Берём первую вкладку или создаём новую
        const pages = await browser.pages();
        const page = pages.length > 0 ? pages[0] : await browser.newPage();

        // Аутентификация прокси
        if (proxyUsername && proxyPassword) {
            await page.authenticate({
                username: proxyUsername,
                password: proxyPassword
            });
            console.log('✅ Прокси аутентификация настроена\n');
        }

        await page.setViewport({ width: 1280, height: 800 });

        // Дополнительная защита: патчим navigator свойства
        await page.evaluateOnNewDocument(() => {
            // 1. Удаляем navigator.webdriver
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined,
            });

            // 2. Подделываем navigator.plugins (Chrome обычно имеет плагины)
            Object.defineProperty(navigator, 'plugins', {
                get: () => {
                    const plugins = [
                        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
                        { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
                    ];
                    plugins.length = 3;
                    return plugins;
                },
            });

            // 3. Подделываем navigator.languages
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en'],
            });

            // 4. Подделываем navigator.platform
            Object.defineProperty(navigator, 'platform', {
                get: () => 'Win32',
            });

            // 5. Подделываем navigator.hardwareConcurrency
            Object.defineProperty(navigator, 'hardwareConcurrency', {
                get: () => 8,
            });

            // 6. Подделываем navigator.deviceMemory
            Object.defineProperty(navigator, 'deviceMemory', {
                get: () => 8,
            });

            // 7. Патчим Permissions.query для notifications
            const originalQuery = window.Permissions.prototype.query;
            window.Permissions.prototype.query = function (parameters) {
                if (parameters.name === 'notifications') {
                    return Promise.resolve({ state: Notification.permission });
                }
                return originalQuery.call(this, parameters);
            };

            // 8. Подделываем WebGL renderer/vendor
            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function (parameter) {
                if (parameter === 37445) return 'Google Inc. (NVIDIA)';
                if (parameter === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)';
                return getParameter.call(this, parameter);
            };

            // 9. chrome.runtime (для прохождения проверок)
            if (!window.chrome) {
                window.chrome = {};
            }
            if (!window.chrome.runtime) {
                window.chrome.runtime = {
                    connect: function() {},
                    sendMessage: function() {},
                };
            }

            // 10. Подделываем Connection API
            Object.defineProperty(navigator, 'connection', {
                get: () => ({
                    effectiveType: '4g',
                    rtt: 50,
                    downlink: 10,
                    saveData: false,
                }),
            });
        });

        // ШАГ 0: Разогрев — сначала заходим на google.com для создания cookies
        console.log('🔥 Разогрев: переход на google.com...');
        try {
            await page.goto('https://www.google.com', {
                waitUntil: 'domcontentloaded',
                timeout: 30000
            });
            await sleep(2000 + Math.random() * 2000);
            
            // Принимаем cookie consent если есть
            try {
                const acceptBtn = await page.evaluate(() => {
                    const buttons = Array.from(document.querySelectorAll('button'));
                    const accept = buttons.find(b => {
                        const text = (b.textContent || '').toLowerCase();
                        return text.includes('accept all') || text.includes('accept') || text.includes('agree') || text.includes('принять');
                    });
                    if (accept) { accept.click(); return true; }
                    return false;
                });
                if (acceptBtn) console.log('   ✅ Cookie consent принят');
            } catch (e) { /* нет consent — ок */ }
            
            await sleep(1000 + Math.random() * 1000);
            console.log('   ✅ Разогрев завершён');
        } catch (e) {
            console.log(`   ⚠️ Разогрев не удался: ${e.message} (продолжаем)`);
        }

        // ШАГ 1: Google Labs Flow
        console.log('🎨 Переход на Google Labs Flow...');
        await page.goto('https://labs.google/fx/tools/flow', {
            waitUntil: 'networkidle2',  // Ждём полной загрузки!
            timeout: 60000  // Увеличен таймаут для медленных прокси
        });
        await sleep(5000);  // Дополнительная пауза для рендеринга
        await debug.capture(page, '01_flow_page');

        // ШАГ 2: Поиск кнопки "Create with Flow"
        console.log('🔍 Поиск кнопки "Create with Flow"...');
        
        // Функция для поиска и клика по кнопке
        const clickCreateButton = async () => {
            // Сначала ищем точное совпадение "Create with Flow"
            const clicked = await page.evaluate(() => {
                // Приоритет 1: Точное совпадение текста
                const allElements = Array.from(document.querySelectorAll('button, a, [role="button"]'));
                
                // Ищем кнопку с точным текстом
                let btn = allElements.find(el => {
                    const text = el.textContent.trim();
                    return text === 'Create with Flow' || text.includes('Create with Flow');
                });
                
                if (btn) {
                    btn.click();
                    return 'Create with Flow';
                }
                
                // Приоритет 2: Кнопка в шапке с текстом Flow
                const headerBtns = document.querySelectorAll('header button, header a, nav button, nav a');
                btn = Array.from(headerBtns).find(el => el.textContent.includes('Flow'));
                if (btn) {
                    btn.click();
                    return 'Flow (header)';
                }
                
                // Приоритет 3: Любая кнопка/ссылка с Flow (но не в footer)
                btn = allElements.find(el => {
                    if (el.closest('footer')) return false;
                    const text = el.textContent.trim();
                    return text.includes('Flow') && text.length < 50; // Не длинные тексты
                });
                if (btn) {
                    btn.click();
                    return 'Flow (any)';
                }
                
                return null;
            });
            
            return clicked;
        };

        // Функция для обработки экрана "Welcome to your new account"
        const handleWelcomeScreen = async () => {
            console.log('🔍 Проверка на экран "Welcome to your new account"...');
            
            // Пытаемся сделать скрин
            try { await debug.capture(page, '08_check_welcome_start'); } catch (e) {}

            try {
                // Пытаемся найти и нажать в течение 10 секунд
                for (let i = 1; i <= 10; i++) {
                    const handled = await page.evaluate(() => {
                        const keywords = ['i understand', 'understand', 'confirm', 'agree', 'accept', 'понимаю', 'принимаю', 'подтверждаю', 'ок'];
                        
                        // 1. Сначала ищем по ID или имени (самый надежный способ для этой формы)
                        const confirmInput = document.getElementById('confirm') || document.querySelector('input[name="confirm"]');
                        if (confirmInput) {
                            const style = window.getComputedStyle(confirmInput);
                            if (style.display !== 'none' && style.visibility !== 'hidden') {
                                confirmInput.click();
                                return 'ID:confirm';
                            }
                        }

                        // Хелпер для получения текста (учитываем value для инпутов)
                        const getText = el => {
                            if (el.tagName === 'INPUT' && (el.type === 'submit' || el.type === 'button')) {
                                return (el.value || '').trim().toLowerCase();
                            }
                            return (el.textContent || '').trim().toLowerCase();
                        };
                        
                        // Глубокий поиск по всем элементам
                        const allElements = document.querySelectorAll('button, input[type="submit"], input[type="button"], div[role="button"], span[role="button"], a');
                        
                        for (const el of allElements) {
                            // Проверяем видимость
                            const style = window.getComputedStyle(el);
                            if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
                            const rect = el.getBoundingClientRect();
                            if (rect.width === 0 || rect.height === 0) continue;

                            const text = getText(el);
                            // Точное совпадение или содержание (для кнопок)
                            if (keywords.some(k => text.includes(k))) {
                                el.click();
                                return el.tagName + ':' + text.slice(0, 20);
                            }
                        }
                        
                        return null;
                    });

                    if (handled) {
                        console.log(`✅ Нажат "${handled}" на экране приветствия`);
                        await sleep(5000); // Ждем перехода
                        try { await debug.capture(page, '09_after_welcome_click_deep'); } catch (e) {}
                        return true;
                    }

                    // Пауза перед следующей попыткой
                    if (i < 10) await sleep(1000);
                }
                
                console.log('   Кнопка welcome screen не найдена после 10 попыток');
            } catch (e) {
                console.log(`⚠️ Ошибка обработки welcome screen: ${e.message}`);
            }
            return false;
        };

        // Функция для обработки заблокированного аккаунта (Disabled Account)
        const handleDisabledScreen = async () => {
            const isDisabled = await page.evaluate(() => {
                if (!document.body) return false;
                const text = (document.body.innerText || '').toLowerCase() + ' ' + (document.body.textContent || '').toLowerCase();
                return text.includes('your account has been disabled') || 
                       text.includes('google workspace policy') ||
                       text.includes('аккаунт отключен');
            });
            
            if (isDisabled) {
                console.log('🚨 ОБНАРУЖЕНА БЛОКИРОВКА АККАУНТА (Disabled)!');
                try { await debug.capture(page, '99_account_disabled'); } catch (e) {}
                console.log('❌ Завершаем работу с кодом 11 (BANNED)');
                process.exit(11); // Специальный код выхода для забаненных аккаунтов
            }
        };

        // Функция для обработки смены пароля (Change Password)
        const handleChangePasswordScreen = async () => {
            console.log('🔍 Проверка на экран "Change Password" / "Create a strong password"...');
            
            try {
                // Ищем любые 2 поля пароля (новое и подтверждение)
                const passwordInputs = await page.evaluate(() => {
                    const inputs = Array.from(document.querySelectorAll('input[type="password"]'));
                    return inputs.filter(el => {
                        const style = window.getComputedStyle(el);
                        return style.display !== 'none' && style.visibility !== 'hidden' && el.offsetWidth > 0;
                    }).length;
                });

                if (passwordInputs === 0) {
                     console.log('   Экран смены пароля не обнаружен (0 полей пароля)');
                     return false;
                }

                if (passwordInputs === 1) {
                    console.log('⚠️ ОБНАРУЖЕНО 1 ПОЛЕ ПАРОЛЯ! Похоже на повторную верификацию ("Verify it\'s you")...');
                    await debug.capture(page, '08_verify_its_you_detected');
                    
                    // Вводим текущий пароль аккаунта
                    await page.type('input[type="password"]', account.password, { delay: 100 });
                    await sleep(500);
                    
                    await debug.capture(page, '08_verify_its_you_filled');
                } else {
                    console.log(`⚠️ ОБНАРУЖЕНО ${passwordInputs} ПОЛЕЙ ПАРОЛЯ! Вероятно, это смена пароля.`);
                    await debug.capture(page, '08_change_password_detected');

                    const NEW_PASSWORD = 'Matkhau@1234@';

                    // Заполняем ВСЕ поля пароля новым паролем
                    await page.evaluate((newPass) => {
                        const inputs = Array.from(document.querySelectorAll('input[type="password"]'));
                        inputs.forEach(el => {
                            const style = window.getComputedStyle(el);
                            if (style.display !== 'none' && style.visibility !== 'hidden' && el.offsetWidth > 0) {
                                el.focus();
                                el.value = newPass;
                                el.dispatchEvent(new Event('input', { bubbles: true }));
                                el.dispatchEvent(new Event('change', { bubbles: true }));
                            }
                        });
                    }, NEW_PASSWORD);
                    
                    console.log(`✅ Заполнено ${passwordInputs} полей пароля`);
                    await sleep(1000);
                    await debug.capture(page, '08_change_password_filled');
                }

                // Кликаем кнопку подтверждения (Next или Change password)
                const clicked = await page.evaluate(() => {
                    // 1. Ищем по тексту
                    const allBtns = Array.from(document.querySelectorAll('button, input[type="submit"], div[role="button"]'));
                    let target = allBtns.find(b => {
                         const val = (b.value || b.textContent || '').toLowerCase().trim();
                         return val === 'next' || 
                                val === 'change password' || 
                                val === 'change' || 
                                val === 'submit' ||
                                val === 'далее' || 
                                val === 'сменить пароль';
                    });
                    
                    // 2. Если не нашли, ищем просто "Next" в любом регистре содержащееся
                    if (!target) {
                        target = allBtns.find(b => {
                             const val = (b.value || b.textContent || '').toLowerCase();
                             return val.includes('next') || val.includes('change password');
                        });
                    }

                    // 3. Если всё ещё нет, берем первую видимую кнопку submit/primary
                    if (!target) {
                        target = document.querySelector('button[type="submit"]') || 
                                 document.querySelector('div[role="button"][id*="Next"]'); 
                    }

                    if (target) {
                        // Проверка видимости
                        const style = window.getComputedStyle(target);
                        if(style.display !== 'none' && style.visibility !== 'hidden') {
                            target.click();
                            return (target.textContent || target.value || 'button').trim();
                        }
                    }
                    return null;
                });

                if (clicked) {
                    console.log(`✅ Нажата кнопка подтверждения: "${clicked}"`);
                    
                    // Ждем навигации
                    await sleep(5000);
                    await debug.capture(page, '08_change_password_done');
                    
                    // ОБНОВЛЯЕМ accounts.json
                    try {
                        const accountsPath = path.join(process.cwd(), 'accounts.json');
                        const accountsData = await fs.readFile(accountsPath, 'utf8');
                        const accounts = JSON.parse(accountsData);
                        
                        let updated = false;
                        for (const acc of accounts) {
                            if (acc.email === account.email) {
                                acc.password = NEW_PASSWORD;
                                updated = true;
                                break;
                            }
                        }
                        
                        if (updated) {
                            await fs.writeFile(accountsPath, JSON.stringify(accounts, null, 4), 'utf8');
                            console.log(`💾 ОБНОВЛЕН ПАРОЛЬ в accounts.json для ${account.email}`);
                        } else {
                            console.log('⚠️ Не удалось найти аккаунт в accounts.json для обновления пароля');
                        }
                    } catch (err) {
                        console.error(`❌ Ошибка обновления accounts.json: ${err.message}`);
                    }

                    return true;
                } else {
                    console.log('❌ Не удалось найти кнопку подтверждения смены пароля');
                }

            } catch (e) {
                console.log(`⚠️ Ошибка обработки change password screen: ${e.message}`);
            }
            return false;
        };

        let buttonClicked = null;
        try {
            // Первая попытка
            buttonClicked = await clickCreateButton();
            
            if (buttonClicked) {
                console.log(`✅ Кнопка найдена и нажата: "${buttonClicked}"`);
            } else {
                console.log('⚠️ Кнопка не найдена с первой попытки, ждём ещё...');
                await sleep(5000);  // Ждём загрузки
                buttonClicked = await clickCreateButton();
                
                if (buttonClicked) {
                    console.log(`✅ Кнопка найдена со второй попытки: "${buttonClicked}"`);
                } else {
                    console.log('⚠️ Кнопка не найдена, пробуем scroll и retry...');
                    // Скроллим вверх страницы
                    await page.evaluate(() => window.scrollTo(0, 0));
                    await sleep(2000);
                    buttonClicked = await clickCreateButton();
                    
                    if (buttonClicked) {
                        console.log(`✅ Кнопка найдена после скролла: "${buttonClicked}"`);
                    } else {
                        console.log('❌ Кнопка "Create with Flow" не найдена!');
                    }
                }
            }
        } catch (error) {
            console.log(`⚠️ Ошибка поиска кнопки: ${error.message}`);
        }

        // Ждём навигации после клика
        if (buttonClicked) {
            console.log('⏳ Ожидание навигации после клика...');
            try {
                await Promise.race([
                    page.waitForNavigation({ waitUntil: 'domcontentloaded', timeout: 15000 }),
                    sleep(10000)  // Максимум 10 секунд
                ]);
            } catch (e) {
                console.log('   Навигация не произошла или завершилась');
            }
            await sleep(2000);

            // Проверяем, не открылась ли новая вкладка
            const allPages = await browser.pages();
            if (allPages.length > 1) {
                // Берём последнюю (самую новую) вкладку
                const newPage = allPages[allPages.length - 1];
                if (newPage !== page) {
                    console.log(`   🔄 Обнаружена новая вкладка (всего: ${allPages.length}), переключаемся...`);
                    // Закрываем старую вкладку
                    try { await page.close(); } catch (e) { /* ignore */ }
                    page = newPage;
                    await page.bringToFront();
                    await sleep(2000);
                    console.log(`   ✅ Переключились на: ${page.url()}`);
                }
            }
        }

        await debug.capture(page, '02_after_button');

        // ШАГ 3: Проверка авторизации и логин
        await sleep(2000);

        // Проверяем есть ли токен (значит залогинены)
        let cookies = await page.cookies();
        const hasSessionToken = cookies.some(c => c.name === '__Secure-next-auth.session-token');

        const currentUrl = page.url();

        if (hasSessionToken && !currentUrl.includes('accounts.google.com')) {
            console.log('✅ Аккаунт уже авторизован (токен найден)');
        } else if (currentUrl.includes('accounts.google.com') || await page.$('input[type="email"]')) {
            // Мы на странице логина
            // Email
            console.log('📝 Ввод email...');
            await debug.capture(page, '03_before_email');
            await page.waitForSelector('input[type="email"], input[type="text"]#identifierId, input[type="text"][name="identifier"]', { timeout: 10000 });
            const emailSelector = await page.$('input[type="email"]') ? 'input[type="email"]' : 'input[type="text"]';
            await page.type(emailSelector, account.email);
            await debug.capture(page, '04_email_filled');

            // Пробуем кликнуть на кнопку Next
            try {
                await page.click('#identifierNext');
                console.log('✅ Кликнули на Next, ждём загрузки страницы пароля...');
            } catch (e) {
                console.log('⚠️ #identifierNext не найден, пробуем альтернативу...');
                await page.evaluate(() => {
                    const buttons = Array.from(document.querySelectorAll('button, div[role="button"]'));
                    const nextBtn = buttons.find(btn => btn.textContent.includes('Next'));
                    if (nextBtn) nextBtn.click();
                });
            }

            // КРИТИЧНО: Ждём навигации на страницу пароля!
            try {
                await page.waitForNavigation({ waitUntil: 'domcontentloaded', timeout: 10000 });
                console.log('✅ Страница пароля загрузилась');
            } catch (e) {
                console.log('⚠️ Навигация по таймауту (продолжаем)');
            }

            await sleep(2000); // дополнительная пауза
            await debug.capture(page, '05_after_email');

            // Password - УЛУЧШЕННЫЙ ВВОД
            console.log('🔑 Ввод пароля...');

            // Ждём появления поля пароля
            await page.waitForSelector('input[type="password"]', { timeout: 20000 });
            console.log('✅ Поле пароля найдено');

            // КРИТИЧНО: Ждём пока поле станет ДЕЙСТВИТЕЛЬНО готовым к вводу
            console.log('   Ожидание готовности поля...');
            await page.waitForFunction(() => {
                const el = document.querySelector('input[type="password"]');
                if (!el) return false;
                const style = window.getComputedStyle(el);
                return !el.disabled && 
                       style.display !== 'none' && 
                       style.visibility !== 'hidden' &&
                       el.offsetWidth > 0 &&
                       el.offsetHeight > 0;
            }, { timeout: 10000 });
            
            await sleep(2000); // дополнительная пауза для анимаций Google

            // Функция надёжного ввода пароля
            const typePasswordReliably = async (password) => {
                // Получаем все поля пароля (может быть несколько)
                const passwordFields = await page.$$('input[type="password"]');
                console.log(`   Найдено полей пароля: ${passwordFields.length}`);
                
                // Берём видимое поле
                let targetField = null;
                for (const field of passwordFields) {
                    const isVisible = await page.evaluate(el => {
                        const style = window.getComputedStyle(el);
                        return style.display !== 'none' && 
                               style.visibility !== 'hidden' &&
                               el.offsetWidth > 0;
                    }, field);
                    if (isVisible) {
                        targetField = field;
                        break;
                    }
                }
                
                if (!targetField) {
                    console.log('   ⚠️ Видимое поле пароля не найдено!');
                    return false;
                }

                // 1. Тройной клик для выделения всего содержимого
                await targetField.click({ clickCount: 3 });
                await sleep(200);
                
                // 2. Удаляем старое содержимое
                await page.keyboard.press('Backspace');
                await sleep(200);
                
                // 3. Фокусируемся
                await targetField.focus();
                await sleep(300);
                
                // 4. Вводим пароль посимвольно с имитацией реального ввода
                for (let i = 0; i < password.length; i++) {
                    const char = password[i];
                    
                    // Имитируем реальные события клавиатуры
                    await page.evaluate((c) => {
                        const field = document.activeElement;
                        if (field && field.tagName === 'INPUT') {
                            // Создаём нативное событие beforeinput
                            const beforeInputEvent = new InputEvent('beforeinput', {
                                inputType: 'insertText',
                                data: c,
                                bubbles: true,
                                cancelable: true
                            });
                            field.dispatchEvent(beforeInputEvent);
                        }
                    }, char);
                    
                    // Печатаем символ
                    await page.keyboard.press(char);
                    
                    // Небольшая случайная задержка для имитации человека
                    await sleep(80 + Math.random() * 70);
                }
                
                await sleep(500);
                
                // Проверяем что ввелось
                const value = await page.$eval('input[type="password"]', el => el.value).catch(() => '');
                return value.length === password.length;
            };

            // СПОСОБ 1: Улучшенный посимвольный ввод
            console.log('   Пробуем способ 1: посимвольный ввод...');
            let passwordSuccess = await typePasswordReliably(account.password);
            let passwordValue = await page.$eval('input[type="password"]', el => el.value).catch(() => '');
            console.log(`   Результат способа 1: длина = ${passwordValue.length}`);

            if (passwordValue.length === 0) {
                // СПОСОБ 2: Клик + keyboard.type с задержкой
                console.log('   Пробуем способ 2: keyboard.type...');
                try {
                    await page.click('input[type="password"]');
                    await sleep(500);
                    await page.keyboard.type(account.password, { delay: 120 });
                    await sleep(500);
                    passwordValue = await page.$eval('input[type="password"]', el => el.value).catch(() => '');
                    console.log(`   Результат способа 2: длина = ${passwordValue.length}`);
                } catch (e) {
                    console.log('   Способ 2 ошибка:', e.message);
                }
            }

            if (passwordValue.length === 0) {
                // СПОСОБ 3: page.type (стандартный)
                console.log('   Пробуем способ 3: page.type...');
                try {
                    await page.focus('input[type="password"]');
                    await sleep(300);
                    await page.type('input[type="password"]', account.password, { delay: 100 });
                    await sleep(500);
                    passwordValue = await page.$eval('input[type="password"]', el => el.value).catch(() => '');
                    console.log(`   Результат способа 3: длина = ${passwordValue.length}`);
                } catch (e) {
                    console.log('   Способ 3 ошибка:', e.message);
                }
            }

            if (passwordValue.length === 0) {
                // СПОСОБ 4: JavaScript с нативными сеттерами (последняя попытка)
                console.log('   Пробуем способ 4: нативный сеттер...');
                await page.evaluate((password) => {
                    const field = document.querySelector('input[type="password"]');
                    if (!field) return;
                    
                    // Используем нативный сеттер через прототип
                    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    ).set;
                    
                    field.focus();
                    nativeInputValueSetter.call(field, password);
                    
                    // Триггерим события
                    field.dispatchEvent(new Event('input', { bubbles: true }));
                    field.dispatchEvent(new Event('change', { bubbles: true }));
                }, account.password);
                
                await sleep(500);
                passwordValue = await page.$eval('input[type="password"]', el => el.value).catch(() => '');
                console.log(`   Результат способа 4: длина = ${passwordValue.length}`);
            }

            // Финальная проверка
            if (passwordValue.length > 0) {
                console.log(`✅ Пароль УСПЕШНО введён! (длина: ${passwordValue.length})`);
            } else {
                console.log('❌ НЕ УДАЛОСЬ ВВЕСТИ ПАРОЛЬ АВТОМАТИЧЕСКИ!');
                console.log('   ВВЕДИТЕ ПАРОЛЬ ВРУЧНУЮ в открытом браузере!');
                console.log('   Ожидаю 30 секунд...');
                await sleep(30000); // даём время ввести вручную
            }

            await debug.capture(page, '06_password_filled');

            await sleep(1000); // пауза перед кликом на Next

            // Пробуем разные селекторы для кнопки Next
            let passwordNextClicked = false;
            const nextButtonSelectors = [
                '#passwordNext',
                'button[type="button"]',
                'div[role="button"]'
            ];

            for (const selector of nextButtonSelectors) {
                try {
                    const button = await page.$(selector);
                    if (button) {
                        const isVisible = await page.evaluate(el => {
                            const style = window.getComputedStyle(el);
                            return style.display !== 'none' && style.visibility !== 'hidden';
                        }, button);

                        if (isVisible) {
                            await button.click();
                            console.log(`✅ Кликнули на кнопку: ${selector}`);
                            passwordNextClicked = true;
                            break;
                        }
                    }
                } catch (e) {
                    continue;
                }
            }

            if (!passwordNextClicked) {
                console.log('⚠️ Пробуем клик через JavaScript...');
                await page.evaluate(() => {
                    const buttons = Array.from(document.querySelectorAll('button, div[role="button"]'));
                    const nextBtn = buttons.find(btn =>
                    btn.textContent.includes('Next') ||
                    btn.id === 'passwordNext'
                    );
                    if (nextBtn) nextBtn.click();
                });
            }

            await sleep(3000);
            await debug.capture(page, '07_after_password');

            // Проверка заблокированного аккаунта (Disabled screen)
            await handleDisabledScreen();

            // 2FA
            console.log(`📱 2FA check: otp_secret=${account.otp_secret ? 'SET (' + account.otp_secret.substring(0, 4) + '...)' : 'NOT SET'}`);
            console.log(`   Current URL: ${page.url()}`);
            if (account.otp_secret) {
                try {
                    console.log('📱 2FA: ожидание экрана верификации...');
                    await debug.capture(page, '07b_before_2fa');
                    
                    // Ждём появление поля ввода OTP (пробуем разные селекторы)
                    const otpSelectors = [
                        'input[type="tel"]',
                        'input#totpPin',
                        'input[name="totpPin"]',
                        'input[name="pin"]',
                        'input[aria-label="Enter code"]',
                    ];
                    
                    let otpInput = null;
                    for (let attempt = 0; attempt < 15; attempt++) {
                        for (const selector of otpSelectors) {
                            otpInput = await page.$(selector);
                            if (otpInput) {
                                console.log(`   ✅ OTP поле найдено: ${selector} (попытка ${attempt + 1})`);
                                break;
                            }
                        }
                        if (otpInput) break;
                        
                        // Также проверяем по тексту страницы
                        const has2FA = await page.evaluate(() => {
                            const text = (document.body.innerText || '').toLowerCase();
                            return text.includes('2-step verification') || 
                                   text.includes('enter code') ||
                                   text.includes('verification code') ||
                                   text.includes('authenticator');
                        });
                        
                        if (has2FA && !otpInput) {
                            // Пробуем найти любой видимый input на странице 2FA
                            otpInput = await page.evaluate(() => {
                                const inputs = Array.from(document.querySelectorAll('input'));
                                const visible = inputs.find(el => {
                                    const style = window.getComputedStyle(el);
                                    return style.display !== 'none' && 
                                           style.visibility !== 'hidden' && 
                                           el.offsetWidth > 0 &&
                                           el.type !== 'hidden' &&
                                           el.type !== 'checkbox';
                                });
                                return visible ? true : false;
                            });
                            if (otpInput === true) {
                                // Найдён видимый input, используем первый видимый
                                otpInput = await page.$('input:not([type="hidden"]):not([type="checkbox"])');
                                if (otpInput) {
                                    console.log(`   ✅ OTP поле найдено через fallback (попытка ${attempt + 1})`);
                                    break;
                                }
                            }
                        }
                        
                        await sleep(1000);
                    }
                    
                    if (otpInput) {
                        const crypto = require('crypto');
                        const base32chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
                        const secretUpper = account.otp_secret.replace(/\s+/g, '').toUpperCase().replace(/=+$/, '');
                        let bits = '';
                        for (const c of secretUpper) bits += base32chars.indexOf(c).toString(2).padStart(5, '0');
                        const secretBytes = Buffer.alloc(Math.floor(bits.length / 8));
                        for (let i = 0; i < secretBytes.length; i++) secretBytes[i] = parseInt(bits.slice(i*8, i*8+8), 2);
                        const timeStep = Math.floor(Date.now() / 1000 / 30);
                        const timeBuffer = Buffer.alloc(8);
                        timeBuffer.writeUInt32BE(Math.floor(timeStep / 0x100000000), 0);
                        timeBuffer.writeUInt32BE(timeStep & 0xFFFFFFFF, 4);
                        const hmac = crypto.createHmac('sha1', secretBytes).update(timeBuffer).digest();
                        const offset = hmac[hmac.length - 1] & 0x0f;
                        const code = String(((hmac.readUInt32BE(offset) & 0x7fffffff) % 1000000)).padStart(6, '0');
                        console.log(`   🔑 Генерация OTP кода: ${code}`);
                        
                        // Кликаем на поле и вводим код
                        await otpInput.click();
                        await sleep(300);
                        await otpInput.type(code, { delay: 100 });
                        await sleep(500);
                        
                        await debug.capture(page, '07c_2fa_filled');
                        
                        // Кликаем Next
                        let nextClicked = false;
                        const nextSelectors = ['#totpNext', 'button[type="button"]', 'div[role="button"]'];
                        for (const sel of nextSelectors) {
                            try {
                                const btn = await page.$(sel);
                                if (btn) {
                                    const isVisible = await page.evaluate(el => {
                                        const style = window.getComputedStyle(el);
                                        const text = (el.textContent || '').toLowerCase();
                                        return style.display !== 'none' && 
                                               style.visibility !== 'hidden' &&
                                               (text.includes('next') || text.includes('далее') || el.id === 'totpNext');
                                    }, btn);
                                    if (isVisible) {
                                        await btn.click();
                                        console.log(`   ✅ Нажата кнопка: ${sel}`);
                                        nextClicked = true;
                                        break;
                                    }
                                }
                            } catch (e) { continue; }
                        }
                        
                        if (!nextClicked) {
                            // Fallback: ищем кнопку Next по тексту
                            await page.evaluate(() => {
                                const btns = Array.from(document.querySelectorAll('button, div[role="button"]'));
                                const next = btns.find(b => {
                                    const text = (b.textContent || '').trim().toLowerCase();
                                    return text === 'next' || text === 'далее';
                                });
                                if (next) next.click();
                            });
                            console.log('   ✅ Нажата кнопка Next (через JS поиск)');
                        }
                        
                        await sleep(5000);
                        await debug.capture(page, '07d_after_2fa');
                        console.log('   ✅ 2FA код введён!');
                    } else {
                        console.log('   ⚠️ Поле OTP не найдено за 15 сек');
                        await debug.capture(page, '07e_2fa_not_found');
                    }
                } catch (error) {
                    console.log(`   ⚠️ 2FA ошибка: ${error.message}`);
                    await debug.capture(page, '07f_2fa_error');
                }
            } else {
                // Даже без otp_secret, ждём немного — вдруг 2FA не потребуется
                await sleep(2000);
            }

            // ШАГ 3.5: Проверка экрана смены пароля (Change Password) - ДО
            await handleChangePasswordScreen();

            // ШАГ 3.6: Проверка экрана "Welcome to your new account"
            await handleWelcomeScreen();

            // ШАГ 3.7: Проверка экрана смены пароля (Change Password) - ПОСЛЕ
            await handleChangePasswordScreen();

            // Ждём редирект
            console.log('⏳ Ожидание редиректа на Flow...');
            await sleep(5000);
            await debug.capture(page, '10_after_login');

            if (page.url().includes('accounts.google.com')) {
                console.log('   Редирект вручную...');
                await page.goto('https://labs.google/fx/tools/flow', { waitUntil: 'domcontentloaded' });
                await sleep(3000);
            }
        } else {
            // Мы на странице Flow, но не залогинены
            console.log('⚠️ На странице Flow, но токена нет - пробуем авторизацию...');

            // Пробуем кликнуть на кнопку снова
            try {
                await page.evaluate(() => {
                    const buttons = Array.from(document.querySelectorAll('button, a'));
                    const flowBtn = buttons.find(btn =>
                    btn.textContent.includes('Create with Flow') ||
                    btn.textContent.includes('Sign in') ||
                    btn.textContent.includes('Get started')
                    );
                    if (flowBtn) {
                        console.log('Кликаю на кнопку:', flowBtn.textContent);
                        flowBtn.click();
                    }
                });
                await sleep(3000);

                // Проверяем попали ли на страницу логина
                if (page.url().includes('accounts.google.com') || await page.$('input[type="email"]')) {
                    console.log('✅ Перешли на страницу логина, авторизуемся...');

                    // Email
                    console.log('📝 Ввод email...');
                    await debug.capture(page, '03_before_email');
                    await page.waitForSelector('input[type="email"], input[type="text"]#identifierId, input[type="text"][name="identifier"]', { timeout: 10000 });
                    const emailSelector2 = await page.$('input[type="email"]') ? 'input[type="email"]' : 'input[type="text"]';
                    await page.type(emailSelector2, account.email);
                    await debug.capture(page, '04_email_filled');

                    // Кликаем на Next
                    try {
                        await page.click('#identifierNext');
                        console.log('✅ Кликнули на Next, ждём загрузки страницы пароля...');
                    } catch (e) {
                        console.log('⚠️ #identifierNext не найден, пробуем альтернативу...');
                        await page.evaluate(() => {
                            const buttons = Array.from(document.querySelectorAll('button, div[role="button"]'));
                            const nextBtn = buttons.find(btn => btn.textContent.includes('Next'));
                            if (nextBtn) nextBtn.click();
                        });
                    }

                    // КРИТИЧНО: Ждём навигации на страницу пароля!
                    try {
                        await page.waitForNavigation({ waitUntil: 'domcontentloaded', timeout: 10000 });
                        console.log('✅ Страница пароля загрузилась');
                    } catch (e) {
                        console.log('⚠️ Навигация по таймауту (продолжаем)');
                    }

                    await sleep(2000);
                    await debug.capture(page, '05_after_email');
                    
                    // Проверка заблокированного аккаунта (Disabled screen)
                    await handleDisabledScreen();

                    // Password - УЛУЧШЕННЫЙ ВВОД
                    console.log('🔑 Ввод пароля...');

                    // Ждём появления поля пароля
                    await page.waitForSelector('input[type="password"]', { timeout: 20000 });
                    console.log('✅ Поле пароля найдено');

                    // КРИТИЧНО: Ждём пока поле станет ДЕЙСТВИТЕЛЬНО готовым к вводу
                    console.log('   Ожидание готовности поля...');
                    await page.waitForFunction(() => {
                        const el = document.querySelector('input[type="password"]');
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        return !el.disabled && 
                               style.display !== 'none' && 
                               style.visibility !== 'hidden' &&
                               el.offsetWidth > 0 &&
                               el.offsetHeight > 0;
                    }, { timeout: 10000 });
                    
                    await sleep(2000); // дополнительная пауза для анимаций Google

                    // Функция надёжного ввода пароля
                    const typePasswordReliably2 = async (password) => {
                        const passwordFields = await page.$$('input[type="password"]');
                        console.log(`   Найдено полей пароля: ${passwordFields.length}`);
                        
                        let targetField = null;
                        for (const field of passwordFields) {
                            const isVisible = await page.evaluate(el => {
                                const style = window.getComputedStyle(el);
                                return style.display !== 'none' && 
                                       style.visibility !== 'hidden' &&
                                       el.offsetWidth > 0;
                            }, field);
                            if (isVisible) {
                                targetField = field;
                                break;
                            }
                        }
                        
                        if (!targetField) {
                            console.log('   ⚠️ Видимое поле пароля не найдено!');
                            return false;
                        }

                        await targetField.click({ clickCount: 3 });
                        await sleep(200);
                        await page.keyboard.press('Backspace');
                        await sleep(200);
                        await targetField.focus();
                        await sleep(300);
                        
                        for (let i = 0; i < password.length; i++) {
                            const char = password[i];
                            await page.evaluate((c) => {
                                const field = document.activeElement;
                                if (field && field.tagName === 'INPUT') {
                                    const beforeInputEvent = new InputEvent('beforeinput', {
                                        inputType: 'insertText',
                                        data: c,
                                        bubbles: true,
                                        cancelable: true
                                    });
                                    field.dispatchEvent(beforeInputEvent);
                                }
                            }, char);
                            await page.keyboard.press(char);
                            await sleep(80 + Math.random() * 70);
                        }
                        
                        await sleep(500);
                        const value = await page.$eval('input[type="password"]', el => el.value).catch(() => '');
                        return value.length === password.length;
                    };

                    // СПОСОБ 1: Улучшенный посимвольный ввод
                    console.log('   Пробуем способ 1: посимвольный ввод...');
                    await typePasswordReliably2(account.password);
                    let passwordValue = await page.$eval('input[type="password"]', el => el.value).catch(() => '');
                    console.log(`   Результат способа 1: длина = ${passwordValue.length}`);

                    if (passwordValue.length === 0) {
                        // СПОСОБ 2: keyboard.type
                        console.log('   Пробуем способ 2: keyboard.type...');
                        try {
                            await page.click('input[type="password"]');
                            await sleep(500);
                            await page.keyboard.type(account.password, { delay: 120 });
                            await sleep(500);
                            passwordValue = await page.$eval('input[type="password"]', el => el.value).catch(() => '');
                            console.log(`   Результат способа 2: длина = ${passwordValue.length}`);
                        } catch (e) {
                            console.log('   Способ 2 ошибка:', e.message);
                        }
                    }

                    if (passwordValue.length === 0) {
                        // СПОСОБ 3: page.type
                        console.log('   Пробуем способ 3: page.type...');
                        try {
                            await page.focus('input[type="password"]');
                            await sleep(300);
                            await page.type('input[type="password"]', account.password, { delay: 100 });
                            await sleep(500);
                            passwordValue = await page.$eval('input[type="password"]', el => el.value).catch(() => '');
                            console.log(`   Результат способа 3: длина = ${passwordValue.length}`);
                        } catch (e) {
                            console.log('   Способ 3 ошибка:', e.message);
                        }
                    }

                    if (passwordValue.length === 0) {
                        // СПОСОБ 4: нативный сеттер
                        console.log('   Пробуем способ 4: нативный сеттер...');
                        await page.evaluate((password) => {
                            const field = document.querySelector('input[type="password"]');
                            if (!field) return;
                            const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                                window.HTMLInputElement.prototype, 'value'
                            ).set;
                            field.focus();
                            nativeInputValueSetter.call(field, password);
                            field.dispatchEvent(new Event('input', { bubbles: true }));
                            field.dispatchEvent(new Event('change', { bubbles: true }));
                        }, account.password);
                        
                        await sleep(500);
                        passwordValue = await page.$eval('input[type="password"]', el => el.value).catch(() => '');
                        console.log(`   Результат способа 4: длина = ${passwordValue.length}`);
                    }

                    // Финальная проверка
                    if (passwordValue.length > 0) {
                        console.log(`✅ Пароль УСПЕШНО введён! (длина: ${passwordValue.length})`);
                    } else {
                        console.log('❌ НЕ УДАЛОСЬ ВВЕСТИ ПАРОЛЬ!');
                        console.log('   ВВЕДИТЕ ПАРОЛЬ ВРУЧНУЮ!');
                        await sleep(30000);
                    }

                    await debug.capture(page, '06_password_filled');

                    await sleep(1000);

                    // Пробуем разные селекторы для кнопки Next
                    let passwordNextClicked = false;
                    const nextButtonSelectors = [
                        '#passwordNext',
                        'button[type="button"]',
                        'button:has-text("Next")',
                        'div[role="button"]'
                    ];

                    for (const selector of nextButtonSelectors) {
                        try {
                            const button = await page.$(selector);
                            if (button) {
                                // Проверяем что кнопка видимая и кликабельная
                                const isVisible = await page.evaluate(el => {
                                    const style = window.getComputedStyle(el);
                                    return style.display !== 'none' && style.visibility !== 'hidden';
                                }, button);

                                if (isVisible) {
                                    await button.click();
                                    console.log(`✅ Кликнули на кнопку: ${selector}`);
                                    passwordNextClicked = true;
                                    break;
                                }
                            }
                        } catch (e) {
                            continue;
                        }
                    }

                    if (!passwordNextClicked) {
                        // Последняя попытка - клик через JavaScript
                        console.log('⚠️ Пробуем клик через JavaScript...');
                        await page.evaluate(() => {
                            const buttons = Array.from(document.querySelectorAll('button, div[role="button"]'));
                            const nextBtn = buttons.find(btn =>
                            btn.textContent.includes('Next') ||
                            btn.id === 'passwordNext' ||
                            btn.getAttribute('jsname')
                            );
                            if (nextBtn) nextBtn.click();
                        });
                    }

                    await sleep(3000);
                    await debug.capture(page, '07_after_password');

                    // Проверка заблокированного аккаунта (Disabled screen)
                    await handleDisabledScreen();

                    // 2FA
                    console.log(`📱 2FA check (flow 2): otp_secret=${account.otp_secret ? 'SET (' + account.otp_secret.substring(0, 4) + '...)' : 'NOT SET'}`);
                    console.log(`   Current URL: ${page.url()}`);
                    if (account.otp_secret) {
                        try {
                            console.log('📱 2FA: ожидание экрана верификации...');
                            await debug.capture(page, '07b_before_2fa');
                            
                            const otpSelectors = [
                                'input[type="tel"]',
                                'input#totpPin',
                                'input[name="totpPin"]',
                                'input[name="pin"]',
                                'input[aria-label="Enter code"]',
                            ];
                            
                            let otpInput = null;
                            for (let attempt = 0; attempt < 15; attempt++) {
                                for (const selector of otpSelectors) {
                                    otpInput = await page.$(selector);
                                    if (otpInput) {
                                        console.log(`   ✅ OTP поле найдено: ${selector} (попытка ${attempt + 1})`);
                                        break;
                                    }
                                }
                                if (otpInput) break;
                                
                                const has2FA = await page.evaluate(() => {
                                    const text = (document.body.innerText || '').toLowerCase();
                                    return text.includes('2-step verification') || 
                                           text.includes('enter code') ||
                                           text.includes('verification code') ||
                                           text.includes('authenticator');
                                });
                                
                                if (has2FA && !otpInput) {
                                    otpInput = await page.$('input:not([type="hidden"]):not([type="checkbox"])');
                                    if (otpInput) {
                                        console.log(`   ✅ OTP поле найдено через fallback (попытка ${attempt + 1})`);
                                        break;
                                    }
                                }
                                
                                await sleep(1000);
                            }
                            
                            if (otpInput) {
                                const crypto = require('crypto');
                                const base32chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
                                const secretUpper = account.otp_secret.replace(/\s+/g, '').toUpperCase().replace(/=+$/, '');
                                let bits = '';
                                for (const c of secretUpper) bits += base32chars.indexOf(c).toString(2).padStart(5, '0');
                                const secretBytes = Buffer.alloc(Math.floor(bits.length / 8));
                                for (let i = 0; i < secretBytes.length; i++) secretBytes[i] = parseInt(bits.slice(i*8, i*8+8), 2);
                                const timeStep = Math.floor(Date.now() / 1000 / 30);
                                const timeBuffer = Buffer.alloc(8);
                                timeBuffer.writeUInt32BE(Math.floor(timeStep / 0x100000000), 0);
                                timeBuffer.writeUInt32BE(timeStep & 0xFFFFFFFF, 4);
                                const hmac = crypto.createHmac('sha1', secretBytes).update(timeBuffer).digest();
                                const offset = hmac[hmac.length - 1] & 0x0f;
                                const code = String(((hmac.readUInt32BE(offset) & 0x7fffffff) % 1000000)).padStart(6, '0');
                                console.log(`   🔑 Генерация OTP кода: ${code}`);
                                
                                await otpInput.click();
                                await sleep(300);
                                await otpInput.type(code, { delay: 100 });
                                await sleep(500);
                                
                                await debug.capture(page, '07c_2fa_filled');
                                
                                let nextClicked = false;
                                const nextSelectors = ['#totpNext', 'button[type="button"]', 'div[role="button"]'];
                                for (const sel of nextSelectors) {
                                    try {
                                        const btn = await page.$(sel);
                                        if (btn) {
                                            const isVisible = await page.evaluate(el => {
                                                const style = window.getComputedStyle(el);
                                                const text = (el.textContent || '').toLowerCase();
                                                return style.display !== 'none' && 
                                                       style.visibility !== 'hidden' &&
                                                       (text.includes('next') || text.includes('далее') || el.id === 'totpNext');
                                            }, btn);
                                            if (isVisible) {
                                                await btn.click();
                                                console.log(`   ✅ Нажата кнопка: ${sel}`);
                                                nextClicked = true;
                                                break;
                                            }
                                        }
                                    } catch (e) { continue; }
                                }
                                
                                if (!nextClicked) {
                                    await page.evaluate(() => {
                                        const btns = Array.from(document.querySelectorAll('button, div[role="button"]'));
                                        const next = btns.find(b => {
                                            const text = (b.textContent || '').trim().toLowerCase();
                                            return text === 'next' || text === 'далее';
                                        });
                                        if (next) next.click();
                                    });
                                    console.log('   ✅ Нажата кнопка Next (через JS поиск)');
                                }
                                
                                await sleep(5000);
                                await debug.capture(page, '07d_after_2fa');
                                console.log('   ✅ 2FA код введён!');
                            } else {
                                console.log('   ⚠️ Поле OTP не найдено за 15 сек');
                                await debug.capture(page, '07e_2fa_not_found');
                            }
                        } catch (error) {
                            console.log(`   ⚠️ 2FA ошибка: ${error.message}`);
                        }
                    } else {
                        await sleep(2000);
                    }

                    // ШАГ 3.5: Проверка экрана "Welcome to your new account"
                    await handleWelcomeScreen();

                    // Проверка заблокированного аккаунта (Disabled screen)
                    await handleDisabledScreen();

                    // Ждём редирект
                    console.log('⏳ Ожидание редиректа на Flow...');
                    await sleep(5000);
                    await debug.capture(page, '10_after_login');
                } else {
                    console.log('⚠️ Не удалось перейти на страницу логина');
                    console.log('   Попробуйте авторизоваться вручную в открытом браузере');
                }
            } catch (error) {
                console.log(`⚠️ Ошибка авторизации: ${error.message}`);
            }
        }

        // ШАГ 4: Извлечение токена
        console.log('🔑 Извлечение session token...');
        cookies = await page.cookies(); // переиспользуем переменную

        for (const cookie of cookies) {
            if (cookie.name === '__Secure-next-auth.session-token') {
                sessionToken = cookie.value;
                console.log('\n✅ ТОКЕН НАЙДЕН!');
                console.log('='.repeat(60));
                console.log(sessionToken);
                console.log('='.repeat(60) + '\n');

                const tokenFile = path.join(profilePath, 'session_token.txt');
                await fs.writeFile(tokenFile, sessionToken, 'utf-8');
                console.log(`💾 Токен сохранён: ${tokenFile}`);
                break;
            }
        }

        if (!sessionToken) {
            console.log('⚠️ Токен не найден');
            console.log(`Доступные cookies: ${cookies.map(c => c.name).join(', ')}`);
        }

        await debug.capture(page, '11_final_state');
        console.log(`\n📁 Скриншоты сохранены в: ${debug.debugDir}`);

        // Готово
        console.log('\n✅ Обработка аккаунта завершена\n');

    } catch (error) {
        console.log(`❌ ОШИБКА: ${error.message}`);
        console.log(error.stack);
    } finally {
        if (browser) {
            try { await browser.close(); } catch (e) { /* ignore */ }
        }
        if (chromeProcess) {
            try { chromeProcess.kill('SIGTERM'); } catch (e) { /* ignore */ }
        }
    }

    return sessionToken;
}

/**
 * Filter accounts by CLI arguments.
 *
 * Supports:
 *   node setup_puppeteer.js                     — all accounts
 *   node setup_puppeteer.js veo-57              — by name (substring)
 *   node setup_puppeteer.js veo-57 veo-63       — multiple names
 *   node setup_puppeteer.js veo-57,veo-63       — comma-separated
 *   node setup_puppeteer.js 1                   — by index (1-based)
 *   node setup_puppeteer.js 1-3                 — range
 *   node setup_puppeteer.js stacey              — by email substring
 */
function filterAccounts(allAccounts, args) {
    if (!args.length) return allAccounts;

    // Flatten comma-separated tokens
    const tokens = args.flatMap(a => a.split(',').map(t => t.trim()).filter(Boolean));
    if (!tokens.length) return allAccounts;

    const selected = [];
    const seen = new Set();

    for (const token of tokens) {
        // Range: "1-3"
        if (/^\d+-\d+$/.test(token)) {
            const [lo, hi] = token.split('-').map(Number);
            for (let idx = lo; idx <= hi; idx++) {
                if (idx >= 1 && idx <= allAccounts.length && !seen.has(idx)) {
                    selected.push(allAccounts[idx - 1]);
                    seen.add(idx);
                }
            }
            continue;
        }

        // Numeric index: "1"
        if (/^\d+$/.test(token)) {
            const idx = Number(token);
            if (idx >= 1 && idx <= allAccounts.length && !seen.has(idx)) {
                selected.push(allAccounts[idx - 1]);
                seen.add(idx);
            }
            continue;
        }

        // Name or email substring (case-insensitive)
        const lower = token.toLowerCase();
        allAccounts.forEach((acc, i) => {
            const idx = i + 1;
            if (seen.has(idx)) return;
            if ((acc.name || '').toLowerCase().includes(lower) ||
                (acc.email || '').toLowerCase().includes(lower)) {
                selected.push(acc);
                seen.add(idx);
            }
        });
    }

    return selected;
}

async function main() {
    console.log(`
    ╔═══════════════════════════════════════════════════════════╗
    ║              НАСТРОЙКА ЧЕРЕЗ PUPPETEER                    ║
    ║          (ЛУЧШИЙ для работы с прокси!)                    ║
    ╚═══════════════════════════════════════════════════════════╝
    `);

    const possiblePaths = [
        path.resolve(__dirname, 'config/accounts.json'),
        path.resolve(__dirname, 'accounts.json'),
        'config/accounts.json',
        '/mnt/user-data/uploads/accounts.json',
        'accounts.json'
    ];

    let allAccounts;
    console.log(`📂 Текущая директория: ${process.cwd()}`);
    console.log(`📂 __dirname: ${__dirname}`);
    for (const configPath of possiblePaths) {
        try {
            console.log(`   Пробуем: ${configPath}`);
            const data = await fs.readFile(configPath, 'utf-8');
            allAccounts = JSON.parse(data);
            console.log(`✅ Загружен: ${configPath}\n`);
            break;
        } catch (error) {
            console.log(`   ❌ ${error.code || error.message}`);
            continue;
        }
    }

    if (!allAccounts) {
        console.log('\n❌ Файл accounts.json не найден ни по одному пути!');
        return;
    }

    console.log(`📋 Всего аккаунтов: ${allAccounts.length}\n`);
    allAccounts.forEach((acc, i) => {
        const proxyInfo = acc.proxy ? ' [прокси]' : '';
        console.log(`  ${i + 1}. ${acc.name} - ${acc.email}${proxyInfo}`);
    });

    // Filter by CLI arguments (skip first two: node, script path)
    const cliArgs = process.argv.slice(2);
    const accountsData = filterAccounts(allAccounts, cliArgs);

    if (cliArgs.length && !accountsData.length) {
        console.log(`\n❌ Аккаунты не найдены по фильтру: ${cliArgs.join(', ')}`);
        console.log('\nИспользование:');
        console.log('  node setup_puppeteer.js                     — все аккаунты');
        console.log('  node setup_puppeteer.js veo-57              — по имени');
        console.log('  node setup_puppeteer.js veo-57 veo-63       — несколько');
        console.log('  node setup_puppeteer.js veo-57,veo-63       — через запятую');
        console.log('  node setup_puppeteer.js 1                   — по номеру (1-based)');
        console.log('  node setup_puppeteer.js 1-3                 — диапазон');
        console.log('  node setup_puppeteer.js stacey              — по части email');
        return;
    }

    if (cliArgs.length) {
        console.log(`\n🎯 Выбрано: ${accountsData.length} из ${allAccounts.length}`);
        accountsData.forEach(acc => console.log(`   → ${acc.name} (${acc.email})`));
    }

    console.log('\nНачинаем настройку...\n');

    const results = {};

    for (let i = 0; i < accountsData.length; i++) {
        console.log(`\n${'#'.repeat(60)}`);
        console.log(`АККАУНТ ${i + 1}/${accountsData.length}`);
        console.log(`${'#'.repeat(60)}`);
        const token = await setupAccount(accountsData[i]);
        if (token) {
            results[accountsData[i].email] = token;
        }
    }

    // Сохраняем токены в config/auth_tokens.json
    console.log('\n📦 Сохранение токенов в config/auth_tokens.json...');
    try {
        // Читаем существующий файл (если есть)
        let existingTokens = {};
        try {
            const existing = await fs.readFile('config/auth_tokens.json', 'utf-8');
            existingTokens = JSON.parse(existing);
        } catch (e) {
            // Файл не существует или пустой - OK
        }
        
        // Объединяем с новыми токенами (новые перезаписывают старые)
        const mergedTokens = { ...existingTokens, ...results };
        
        // Сохраняем
        await fs.writeFile('config/auth_tokens.json', JSON.stringify(mergedTokens, null, 2), 'utf-8');
        console.log(`✅ Сохранено ${Object.keys(results).length} новых токенов`);
        console.log(`   Всего токенов в файле: ${Object.keys(mergedTokens).length}`);
    } catch (error) {
        console.log(`❌ Ошибка сохранения auth_tokens.json: ${error.message}`);
    }

    console.log('\n🎉 ГОТОВО!');
}

main().catch(console.error);