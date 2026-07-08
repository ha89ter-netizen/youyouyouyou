"""
AI-стратегия: в отличие от rule_based.py, здесь нет жёсткой формулы.
Модель получает "снепшот" рынка (цены, объёмы, funding, open interest,
недавние сделки) и сама решает, есть ли выгодная возможность.

Использует OpenAI Chat Completions API (response_format=json_object —
модель гарантированно вернёт валидный JSON, а не текст вокруг него).

КРИТИЧЕСКИ ВАЖНО: ответ модели дополнительно парсится и валидируется вручную.
Если модель не смогла выдать валидный JSON по нашей схеме или превысила
разумные значения (например, confidence вне 0..1, stop_loss в 500%) —
сигнал отбрасывается как HOLD. Это ЕЩЁ один защитный слой поверх Risk
Manager: галлюцинация модели физически не может пройти дальше парсинга,
а даже если пройдёт валидацию — Risk Manager дополнительно обрежет размер/плечо.
"""

import json
import logging
from typing import Optional, Dict, Any

import requests

from config.settings import BybitConfig
from strategy.signal import Signal, Action

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты — модуль анализа рынка криптодеривативов в составе торговой системы.
Твоя задача: посмотреть на снепшот рынка и решить, есть ли сейчас выгодная
возможность для сделки по данному инструменту.

Снепшот содержит:
- last_price, price_change_pct_last_20/50_candles, high_20, low_20, avg_volume_20,
  volatility_pct — базовая динамика цены и объёма (15-минутные свечи).
- funding_rate и funding_trend — текущая ставка финансирования и тренд по последним
  8 периодам. Устойчиво растущий funding = рынок всё больше перекошен в лонги
  (дорого удерживать лонг, риск сквиза шортов при развороте, и наоборот).
- open_interest_trend — изменение открытого интереса. Растущий OI + растущая цена =
  новые деньги в лонг, сильный тренд. Растущий OI + падающая цена = усиление шортов.
  Падающий OI = закрытие позиций, тренд слабеет, разворот менее надёжен.
- orderbook.bid_ask_imbalance — дисбаланс объёма в топе стакана. >0 значит давление
  на покупку сильнее, <0 — на продажу. spread_pct — текущий спред (широкий спред
  = низкая ликвидность, осторожнее с размером и ожидаемым проскальзыванием).
- trade_flow_last_minutes.imbalance — то же самое, но по РЕАЛЬНО исполненным сделкам
  за последние минуты, не по стакану. Это более надёжный сигнал реального давления,
  чем orderbook imbalance (стакан можно двигать без реальных сделок).
- liquidations_last_hour — количество и объём принудительных ликвидаций за час.
  Всплеск long_liquidations часто означает локальную капитуляцию лонгов (возможен
  отскок). Всплеск short_liquidations — шорт-сквиз (может продолжиться рост).

- indicators — технические индикаторы, ПОСЧИТАННЫЕ ТОЧНО (не нужно пересчитывать
  самому по сырым ценам, доверяй этим числам):
  - rsi (0-100): >70 перекуплен, <30 перепродан. rsi_prev — значение шагом раньше
    (полезно видеть, выходит ли RSI из зоны экстремума прямо сейчас).
  - macd_histogram и macd_histogram_prev: пересечение нуля — смена импульса.
    Положительный и растущий — усиление бычьего импульса, и наоборот.
  - bollinger_position (0-1, может выходить за границы): 0 = цена на нижней
    полосе Боллинджера, 1 = на верхней. Около 0 или 1 — цена у края диапазона.
  - atr и atr_pct_of_price: средний размах движения. Высокий atr_pct_of_price —
    рынок сейчас волатильный, учитывай это при выборе stop_loss_pct (в волатильном
    рынке слишком узкий стоп выбьет позицию на обычном шуме).

- trend_filter: старший тренд по EMA50/EMA200 на 15-минутных свечах —
  "long" | "short" | "neutral" | null (недостаточно данных, ещё не накопилось
  200+ свечей). Это ИНФОРМАЦИОННОЕ поле: финальная система ВСЕГДА блокирует
  твой сигнал, если он идёт против trend_filter (когда он не null и не
  neutral) — уже после твоего ответа, ты не можешь на это повлиять. Поэтому
  если видишь явное противоречие (например, trend_filter="short", а данные
  тянут тебя к open_long) — смело отвечай "hold" вместо контр-трендового
  сигнала: он всё равно будет отклонён, а честный hold не тратится впустую
  на объяснение того, что заранее известно.

Правила:
- Ты НЕ исполняешь сделки напрямую — только предлагаешь сигнал. Финальное решение
  и все риск-лимиты применяет отдельный компонент системы.
- Не полагайся на один показатель — ищи, когда несколько сигналов указывают
  в одну сторону (например: funding растёт + OI растёт + trade_flow в лонг —
  это сильнее, чем просто funding растёт).
- Будь консервативен: если сигналы противоречат друг другу или неочевидны —
  отвечай "hold". Пропущенная возможность дешевле, чем убыточная сделка.
- Если каких-то полей нет (null) — значит для них ещё недостаточно данных
  в системе, не выдумывай значения, просто опирайся на то, что есть.
- confidence отражай честно: 0.5 = слабый сигнал, 0.8+ = сильный, когда
  несколько независимых сигналов согласованы.
- Всегда указывай stop_loss_pct — без него сигнал будет отклонён.

Отвечай СТРОГО в формате JSON:
{
  "action": "open_long" | "open_short" | "hold",
  "confidence": 0.0-1.0,
  "reason": "краткое объяснение на русском, 1-2 предложения, укажи какие именно сигналы совпали",
  "stop_loss_pct": число (обязательно, если action != hold),
  "take_profit_pct": число (опционально)
}
"""


class OpenAIMarketAnalyst:
    name = "ai:openai"

    def __init__(self, cfg: BybitConfig):
        if not cfg.openai_api_key:
            raise RuntimeError(
                "Для AI-стратегии нужен OPENAI_API_KEY в переменных окружения."
            )
        self.cfg = cfg
        self.api_url = "https://api.openai.com/v1/chat/completions"

    def generate_signal(self, symbol: str, market_snapshot: Dict[str, Any]) -> Optional[Signal]:
        user_prompt = (
            f"Инструмент: {symbol}\n\n"
            f"Снепшот рынка (JSON):\n{json.dumps(market_snapshot, ensure_ascii=False, indent=2)}\n\n"
            "Проанализируй и верни решение в требуемом JSON-формате."
        )

        try:
            raw = self._call_openai(user_prompt)
            decision = self._parse_response(raw, symbol)
        except Exception:
            logger.exception("Ошибка при получении/разборе решения ИИ для %s", symbol)
            return Signal(symbol=symbol, action=Action.HOLD, source=self.name,
                           reason="Ошибка AI-модуля, безопасный HOLD")

        if decision is None:
            return Signal(symbol=symbol, action=Action.HOLD, source=self.name,
                           reason="Не удалось разобрать ответ ИИ, безопасный HOLD")

        return decision

    # ------------------------------------------------------------------

    def _call_openai(self, user_prompt: str) -> str:
        resp = requests.post(
            self.api_url,
            headers={
                "Authorization": f"Bearer {self.cfg.openai_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.cfg.ai_model,
                "max_tokens": 500,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    def _parse_response(self, raw_text: str, symbol: str) -> Optional[Signal]:
        cleaned = raw_text.strip()
        # На случай, если модель всё же обернула ответ в ```json ... ```
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            cleaned = cleaned.replace("json\n", "", 1).strip()

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            logger.error("ИИ вернул не-JSON: %s", raw_text[:300])
            return None

        action_str = data.get("action", "hold")
        try:
            action = Action(action_str)
        except ValueError:
            logger.error("ИИ вернул неизвестное action=%s", action_str)
            return None

        confidence = data.get("confidence", 0.5)
        if not isinstance(confidence, (int, float)) or not (0.0 <= confidence <= 1.0):
            logger.warning("ИИ вернул некорректный confidence=%s, обнуляю сигнал", confidence)
            return None

        stop_loss_pct = data.get("stop_loss_pct")
        if action != Action.HOLD and not stop_loss_pct:
            logger.warning("ИИ предложил сделку без stop_loss_pct — отклоняю сигнал")
            return None

        # Здравый предохранитель: даже если ИИ предложит безумный SL, режем его тут же
        if stop_loss_pct is not None:
            stop_loss_pct = max(0.1, min(float(stop_loss_pct), 10.0))

        take_profit_pct = data.get("take_profit_pct")
        if take_profit_pct is not None:
            take_profit_pct = max(0.1, min(float(take_profit_pct), 30.0))

        return Signal(
            symbol=symbol,
            action=action,
            source=self.name,
            confidence=float(confidence),
            reason=str(data.get("reason", ""))[:500],
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
        )
