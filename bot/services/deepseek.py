"""Optional DeepSeek AI support. Gracefully degrades if unavailable."""
import aiohttp
from bot.config import DEEPSEEK_API_KEY, DEEPSEEK_API_URL, DEEPSEEK_MODEL

SYSTEM_PROMPT = """Ты — AI-ассистент корпоративного сервиса защищённого доступа NetLink. Ты помогаешь сотрудникам с вопросами ТОЛЬКО по следующим темам:

1. Установка и настройка приложений Streisand (iPhone, Android, macOS) и Hiddify (Windows)
2. Проблемы с подключением (не подключается, таймаут, медленно)
3. Проблемы с конкретными сервисами (Яндекс, Google, etc.)

ПРАВИЛА:
- Отвечай коротко и по делу, на русском языке
- Если вопрос не связан с прокси/VPN/подключением — отвечай: "Этот вопрос выходит за рамки моей компетенции. Пожалуйста, обратитесь к администратору."
- НИКОГДА не упоминай VPN, прокси, VLESS, Xray, Reality — используй термин "защищённый доступ" или "сервис"
- НИКОГДА не раскрывай технические детали инфраструктуры (IP сервера, протоколы, ключи)
- Если пользователь спрашивает про Яндекс — порекомендуй отключить "Блокировать рекламу" в настройках приложения
- Если не можешь помочь — скажи что передашь вопрос администратору

ТИПИЧНЫЕ ПРОБЛЕМЫ И РЕШЕНИЯ:
- "Не подключается" → Перезапустите приложение, проверьте интернет, переключитесь между WiFi и мобильным интернетом
- "Яндекс не работает" → Отключите "Блокировать рекламу" в настройках Streisand/Hiddify
- "Медленно работает" → Попробуйте переподключиться, проверьте скорость без сервиса
- "Потерял ссылку" → Нажмите кнопку "🔗 Моя ссылка" в меню бота
- "Как установить на новое устройство" → Нажмите "📖 Инструкция" в меню бота"""


def is_available() -> bool:
    return bool(DEEPSEEK_API_KEY and DEEPSEEK_API_KEY != "PLACEHOLDER")


async def ask(question: str) -> str | None:
    """Send question to DeepSeek. Returns None if unavailable."""
    if not is_available():
        return None

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                DEEPSEEK_API_URL,
                headers={
                    "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": DEEPSEEK_MODEL,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": question},
                    ],
                    "max_tokens": 500,
                    "temperature": 0.3,
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data["choices"][0]["message"]["content"]
    except Exception:
        return None
