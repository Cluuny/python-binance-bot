import os
import json
import pandas as pd
from dotenv import load_dotenv


def load_json():
    with open('config.json', 'r') as f:
        data = json.load(f)

    flat_data = {}

    flat_data.update({
        'symbol': data['symbol'],
        'timeframe': data['timeframe'],
        'risk_per_trade': data['risk_per_trade'],
        'max_concurrent_trades': data['max_concurrent_trades'],
        'stop_loss_pct': data['stop_loss_pct'],
        'take_profit_pct': data['take_profit_pct']
    })

    flat_data.update(data['strategy_configs'])

    config_df = pd.DataFrame([flat_data])
    return config_df

class Config:
    def __init__(self):
        load_dotenv()
        self.api_key = os.getenv('API_KEY')
        self.api_secret = os.getenv('API_SECRET')
        self.data = load_json()


