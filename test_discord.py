from src.discord_alerts import send_alert

if __name__ == "__main__":
    send_alert(
        title="System online",
        description="Test message from the rebuilt alert stack.",
        color=0x00AAFF,
        alert_type="health",
        alert_key="health:test",
        skip_dedupe=True,
    )
