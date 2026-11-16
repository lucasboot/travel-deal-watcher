from __future__ import annotations

from decimal import Decimal

from ..models import FareObservation, PromotionPost


def format_fare_alert(fare: FareObservation, evaluation: dict) -> str:
    outbound_stops = fare.transfers_outbound
    return_stops = fare.transfers_return
    route = f"{fare.origin} → {fare.destination} → {fare.origin}"
    price = _money(fare.total_price, fare.currency)
    per_person = _money(fare.price_per_person, fare.currency)
    stops = (
        f"Conexões: ida {outbound_stops if outbound_stops is not None else '?'}; "
        f"volta {return_stops if return_stops is not None else '?'}"
    )
    layovers = [*fare.layovers_outbound, *fare.layovers_return]
    if layovers:
        windows = ", ".join(f"{item.airport} {item.duration_minutes // 60}h{item.duration_minutes % 60:02d}" for item in layovers)
        stops += f"\nParadas: {windows}"
    baggage = "; ".join(fare.baggage_notes) if fare.baggage_notes else "Bagagem: condições não informadas"
    link = fare.link or "Consulte a fonte para confirmar a oferta."
    return (
        "✈️ Oportunidade de passagem\n"
        f"Rota: {route}\n"
        f"Datas: {fare.departure_date:%d/%m/%Y} – {fare.return_date:%d/%m/%Y} "
        f"({(fare.return_date - fare.departure_date).days} dias)\n"
        f"Preço total: {price} | por pessoa: {per_person}\n"
        f"Fonte: {fare.source} — consulta recente\n"
        f"{stops}\n"
        f"{baggage}\n"
        f"Motivo: abaixo do limite configurado ({_money(Decimal(evaluation['price_threshold']), fare.currency)})\n"
        f"Disponibilidade: preço observado recentemente; confirme no vendedor.\n"
        f"{link}"
    )


def format_promotion(post: PromotionPost) -> str:
    price = (
        f"Preço citado: {_money(post.mentioned_price, post.currency or 'BRL')}\n"
        if post.mentioned_price is not None
        else "Preço: não informado na publicação\n"
    )
    published = (
        f"Publicada: {post.published_at:%d/%m/%Y %H:%M UTC}\n"
        if post.published_at
        else "Data da publicação: não informada\n"
    )
    return (
        "📰 Publicação de promoção para um destino monitorado\n"
        f"{post.title}\n"
        f"Destino identificado: {post.destination_match}\n"
        f"{price}{published}"
        "Verificação: publicação ainda não verificada como tarifa disponível.\n"
        f"Fonte: {post.source}\n{post.url}"
    )


def format_daily_digest(fares: list[FareObservation], promotions: list[PromotionPost]) -> str:
    lines = ["📊 Resumo diário Travel Deal Watcher"]
    if fares:
        lines.append("\nPreços indicativos:")
        for fare in sorted(fares, key=lambda item: item.price_per_person)[:10]:
            lines.append(
                f"• {fare.origin}–{fare.destination}, {fare.departure_date:%d/%m/%Y} a "
                f"{fare.return_date:%d/%m/%Y}: {_money(fare.price_per_person, fare.currency)}/pessoa "
                f"({fare.source}, {fare.confidence})"
            )
    if promotions:
        lines.append("\nPublicações recentes ainda não verificadas:")
        lines.extend(f"• {post.title}\n  {post.url}" for post in promotions[:10])
    if not fares and not promotions:
        lines.append("Nenhuma oportunidade nova registrada no período.")
    return "\n".join(lines)


def _money(value: Decimal, currency: str) -> str:
    if currency.upper() == "BRL":
        return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{value:,.2f} {currency.upper()}"
