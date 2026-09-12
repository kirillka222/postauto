"""
Переписывание новостного поста в нужном стиле через Groq API.
"""
import os
from dotenv import load_dotenv
from groq import AsyncGroq

load_dotenv()

client = AsyncGroq(api_key=os.environ["GROQ_API_KEY"])
MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")

# Настройте этот промпт под стиль вашего канала.
SYSTEM_PROMPT = """\
Ты — редактор новостного Telegram-канала. Твоя задача — переписать входящий \
новостной пост своими словами, сохранив все факты, но полностью изменив \
формулировки и структуру предложений.

Правила:
- Не копируй фразы дословно из исходного текста.
- Пиши живо, но по-журналистски, без вымышленных деталей.
- Убери упоминания и ссылки на источник, если они есть в тексте.
- Не добавляй факты, которых нет в оригинале.
- Сохрани длину примерно как в оригинале (не растягивай и не сокращай сильно).
- Отвечай ТОЛЬКО готовым текстом поста, без пояснений и кавычек.
- Не добавляй никаких подписей, приписок "подписывайтесь" и т.п. — это
  добавляется отдельно после переписывания.
- Используй уместные и подходящие отступы для визуальной красоты оформления текста.
"""


async def rewrite_post(original_text: str) -> str:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Исходный пост:\n\n{original_text}"},
    ]
    try:
        response = await client.chat.completions.create(model=MODEL, messages=messages)
    except TypeError:
        # На случай совсем старой версии SDK, ожидающей другое имя параметра —
        # но в норме лимит токенов не обязателен, дефолта достаточно.
        response = await client.chat.completions.create(
            model=MODEL, messages=messages, max_tokens=1024
        )
    return response.choices[0].message.content.strip()