# SOLGPT
GPT speaks Solana now, leveraging the power of OpenAI models with Solana RPCs

# SolanaGPT API

SolanaGPT is a FastAPI backend that:
- Fetches token prices on Solana
- Checks wallet balances
- Simulates swaps

## Local Dev

1. Create a .env file:
```
HELIUS_API_KEY=your_key_here
```

2. Install and run:
```
pip install -r requirements.txt
uvicorn main:app --reload
```
