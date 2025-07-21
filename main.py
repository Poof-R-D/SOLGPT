from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import requests
from decimal import Decimal
from functools import lru_cache
from random import shuffle
import os

app = FastAPI(
    title="SolanaGPT",
    description="Poof Labs Solana degen trading assistant",
    version="1.0",
    servers=[{"url": "https://solgpt-production-e0e4.up.railway.app"}]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

# API keys and endpoints configuration
HELIUS_RPC_API_KEY = os.getenv("HELIUS_RPC_API_KEY", "a8b4eafc-da15-47d1-89a4-f3492ae7406a")
HELIUS_METADATA_API_KEY = os.getenv("HELIUS_METADATA_API_KEY", "a8b4eafc-da15-47d1-89a4-f3492ae7406a")
ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY", "YOUR_API_KEY")
SYNDICA_API_KEY = os.getenv("SYNDICA_API_KEY", "YOUR_API_KEY")
PUMPFUN_API_BASE = os.getenv("PUMPFUN_API_BASE", "https://frontend-api.pump.fun")

# Solana RPC endpoints (including some that require API keys)
RPC_ENDPOINTS = [
    f"https://rpc.helius.xyz/?api-key={HELIUS_RPC_API_KEY}",
    "https://mainnet.helius-rpc.com",
    "https://api.mainnet-beta.solana.com",
    "https://solana-api.projectserum.com",
    "https://rpc.ankr.com/solana",
    "https://solana.rpcpool.com",
    f"https://solana-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}",
    "https://mainnet.rpc.solana.com",
    "https://solana-mainnet.rpc.extrnode.com",
    "https://api.metaplex.solana.com",
    f"https://solana-api.syndica.io/access-token/{SYNDICA_API_KEY}/rpc"
]

JUPITER_TOKEN_INFO_URL = "https://tokens.jup.ag/token/"
JUPITER_PRICE_URL = "https://api.jup.ag/price/v2?ids="
JUPITER_TOKEN_LIST_URL = "https://token.jup.ag/all"
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

def get_rpc_response(payload: dict):
    """Try the list of RPC endpoints until one returns a valid result."""
    rpc_list = RPC_ENDPOINTS[:]
    shuffle(rpc_list)
    for url in rpc_list:
        try:
            resp = requests.post(url, json=payload, timeout=5)
            data = resp.json()
            if data.get('result') is not None:
                return data
        except Exception:
            continue
    # If none succeeded:
    raise Exception("All RPC endpoints failed or timed out")

def fetch_basic_token_info(mint: str):
    """Fetch token account info to get the owner program (checks if SPL Token)."""
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getAccountInfo",
        "params": [mint, {"encoding": "jsonParsed"}]
    }
    try:
        data = get_rpc_response(payload)
        owner = data.get("result", {}).get("value", {}).get("owner")
        return owner
    except Exception:
        return None

def helius_token_metadata(mint: str):
    """Get richer metadata from Helius API for a token (if Jupiter has no info)."""
    try:
        url = f"https://api.helius.xyz/v0/tokens/metadata?mint={mint}&api-key={HELIUS_METADATA_API_KEY}"
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and data:
                return data[0]  # Return the first metadata result
        return None
    except Exception:
        return None

@lru_cache(maxsize=1)
def get_jupiter_token_map():
    """Fetch the token map from Jupiter (maps symbol -> mint address)."""
    try:
        resp = requests.get(JUPITER_TOKEN_LIST_URL, timeout=10)
        tokens = resp.json()
        return {t['symbol'].lower(): t['address'] for t in tokens if 'symbol' in t and 'address' in t}
    except Exception:
        return {}

def get_token_mint_from_symbol(symbol: str) -> str:
    """Resolve a token symbol to its Solana mint address using Jupiter's token list."""
    token_map = get_jupiter_token_map()
    mint = token_map.get(symbol.lower())
    if not mint:
        raise HTTPException(status_code=404, detail=f"Mint not found for symbol '{symbol}'")
    return mint

def resolve_to_mint(token_input: str) -> str:
    """
    Resolve a user-provided token identifier to a mint address.
    If the input looks like a mint address (length >= 32 characters), return it directly.
    Otherwise, treat it as a token symbol and resolve via Jupiter.
    """
    if len(token_input) >= 32:
        return token_input
    return get_token_mint_from_symbol(token_input)

@app.get("/swap")
def simulate_swap(input_mint: str, output_mint: str, amount: float):
    """Simulate a token swap using Jupiter aggregator and return quote details."""
    # Prepare raw amount for Jupiter API (lamports for SOL, smallest units for others)
    in_mint = input_mint
    out_mint = output_mint
    if in_mint == "So11111111111111111111111111111111111111112":
        raw_amount = int(amount * 1e9)  # Convert SOL amount to lamports
    else:
        try:
            raw_amount = int(amount)
        except Exception:
            raw_amount = None
    if raw_amount is None:
        raise HTTPException(status_code=422, detail="Invalid amount")
    # Request a swap quote from Jupiter's public API
    quote_url = (
        "https://lite-api.jup.ag/quote"
        f"?inputMint={in_mint}&outputMint={out_mint}"
        f"&amount={raw_amount}&slippageBps=50&restrictIntermediateTokens=true"
    )
    try:
        qresp = requests.get(quote_url, timeout=5)
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="Jupiter quote request failed")
    if qresp.status_code != 200:
        raise HTTPException(status_code=502, detail="Jupiter API returned an error")
    quote = qresp.json()
    if not quote or "outAmount" not in quote:
        raise HTTPException(status_code=502, detail="Invalid quote response")
    # Parse quote to extract output amount and route
    out_amount = int(quote["outAmount"])
    route_steps = []
    for step in quote.get("routePlan", []):
        swap_info = step.get("swapInfo", {})
        input_mint_addr = swap_info.get("inputMint")
        # Convert known mint addresses to symbols or short-form for readability
        def mint_to_symbol(mint_addr: str) -> str:
            common = {
                "So11111111111111111111111111111111111111112": "SOL",
                "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
                "Es9vMFrzaCjLwSX5ae4Ew9WVeEKXZotPwX3hPJJrEvDw": "USDT"
            }
            if mint_addr in common:
                return common[mint_addr]
            # Return a shortened representation of the address for other tokens
            return f"{mint_addr[:4]}…{mint_addr[-4:]}" if mint_addr else "UNK"
        route_steps.append(mint_to_symbol(input_mint_addr))
    # Append final output token symbol to route
    route_steps.append(mint_to_symbol(out_mint))
    route_str = " -> ".join(route_steps)
    return {
        "input_amount": f"{amount} {route_steps[0]}",
        "output_estimate": f"{out_amount:,} {route_steps[-1]}",
        "slippage": "0.5%",  # 50 bps slippage
        "route": route_str,
        "platform": "Jupiter Aggregator"
    }

@app.get("/resolve")
def resolve_symbol(symbol: str):
    """Resolve a token symbol to its Solana mint address using Jupiter's token list."""
    mint = resolve_to_mint(symbol)
    return {"symbol": symbol.upper(), "mint": mint}

@app.get("/balances/{address}")
def get_balances(address: str):
    """Get the SOL balance and all SPL token balances for a given wallet address."""
    result = {"sol": None, "tokens": []}
    # Fetch SOL balance (in lamports)
    balance_payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getBalance",
        "params": [address]
    }
    try:
        balance_data = get_rpc_response(balance_payload)
    except Exception as e:
        return {"error": "Unable to fetch SOL balance", "details": str(e)}
    lamports = balance_data.get("result", {}).get("value", 0)
    sol_amount = lamports / 1e9  # Convert lamports to SOL
    # Fetch all SPL token accounts for the address
    token_payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            address,
            {"programId": TOKEN_PROGRAM_ID},
            {"encoding": "jsonParsed"}
        ]
    }
    try:
        token_data = get_rpc_response(token_payload)
    except Exception as e:
        # If token accounts lookup fails, return SOL amount and an error message
        result["sol"] = {"amount": sol_amount, "price": None, "usd_value": None}
        result["tokens"] = []
        result["error"] = "Token account lookup failed"
        return result
    accounts = token_data.get("result", {}).get("value", [])
    token_list = []
    for acct in accounts:
        info = acct.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
        token_amount_info = info.get("tokenAmount", {})
        # Determine token amount with proper decimals
        amount = None
        if token_amount_info.get("uiAmountString") is not None:
            # Use high-precision string if available
            try:
                amount = Decimal(token_amount_info["uiAmountString"])
            except Exception:
                amount = None
        elif token_amount_info.get("uiAmount") is not None:
            amount = Decimal(str(token_amount_info["uiAmount"]))
        else:
            try:
                raw = Decimal(token_amount_info.get("amount", "0"))
                decimals = int(token_amount_info.get("decimals", 0))
                amount = raw / (Decimal(10) ** decimals)
            except Exception:
                amount = None
        if not amount or amount == 0:
            continue  # Skip empty or zero balances
        token_list.append({
            "mint": info.get("mint"),
            "amount": amount
        })
    # Prepare list of mints (including SOL as WSOL) to fetch prices for
    mint_addresses = [t["mint"] for t in token_list]
    WSOL_MINT = "So11111111111111111111111111111111111111112"
    mint_addresses.append(WSOL_MINT)
    prices = {}
    if mint_addresses:
        ids_param = ",".join(mint_addresses)
        try:
            price_resp = requests.get(f"{JUPITER_PRICE_URL}{ids_param}", timeout=5)
            price_data = price_resp.json()
            prices = price_data.get("data", {})
        except Exception:
            prices = {}
    # Assemble token balance results with pricing
    tokens_output = []
    for token in token_list:
        mint = token["mint"]
        amt = token["amount"]
        # Default name and symbol as unknown (will try to fetch metadata below)
        name = "Unknown Token"
        symbol = mint[:4] + "..." + mint[-4:]
        daily_volume = None
        # Try to get token metadata (name, symbol, volume) from Jupiter's token info
        try:
            meta_resp = requests.get(f"{JUPITER_TOKEN_INFO_URL}{mint}", timeout=5)
            if meta_resp.status_code == 200:
                meta = meta_resp.json()
                name = meta.get("name") or name
                symbol = meta.get("symbol") or symbol
                daily_volume = meta.get("daily_volume")
        except Exception:
            pass
        # Get price and USD value if available
        price = None
        usd_value = None
        if mint in prices:
            price_str = prices[mint].get("price")
            if price_str is not None:
                try:
                    price = float(price_str)
                except Exception:
                    # Handle cases where price might be in scientific notation or string
                    try:
                        price = float(Decimal(price_str))
                    except Exception:
                        price = None
        if price is not None:
            usd_value = float(Decimal(str(price)) * amt)
        tokens_output.append({
            "mint": mint,
            "name": name,
            "symbol": symbol,
            "amount": float(amt),
            "price": price,
            "usd_value": usd_value,
            "daily_volume": daily_volume
        })
    # Fetch SOL price (from WSOL entry) if available for total SOL value
    sol_price = None
    sol_usd_value = None
    if WSOL_MINT in prices:
        price_str = prices[WSOL_MINT].get("price")
        if price_str is not None:
            try:
                sol_price = float(price_str)
            except Exception:
                try:
                    sol_price = float(Decimal(price_str))
                except Exception:
                    sol_price = None
    if sol_price is not None:
        sol_usd_value = sol_price * sol_amount
    # Prepare final result
    result["sol"] = {
        "amount": sol_amount,
        "price": sol_price,
        "usd_value": sol_usd_value
    }
    result["tokens"] = tokens_output
    return result

@app.get("/transaction/{signature}")
def get_transaction(signature: str):
    """Get a human-readable summary of a Solana transaction by its signature."""
    tx_payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getTransaction",
        "params": [signature, {"encoding": "jsonParsed"}]
    }
    try:
        tx_data = get_rpc_response(tx_payload)
    except Exception as e:
        return {"error": "Unable to fetch transaction", "details": str(e)}
    if not tx_data.get("result"):
        return {"error": "Transaction not found"}
    tx = tx_data["result"]
    summary_lines = []
    # Check transaction status and fee
    if "meta" in tx:
        if tx["meta"].get("err"):
            summary_lines.append("**Transaction Status**: Failed")
        else:
            summary_lines.append("**Transaction Status**: Success")
        fee = tx["meta"].get("fee")
        if fee is not None:
            summary_lines.append(f"**Fee Paid**: {fee} lamports")
    # Parse each instruction in the transaction
    if "transaction" in tx and "message" in tx["transaction"]:
        instructions = tx["transaction"]["message"].get("instructions", [])
        for idx, instr in enumerate(instructions, start=1):
            program = instr.get("program") or instr.get("programId")
            if program == "spl-token" and "parsed" in instr:
                parsed = instr["parsed"]
                instr_type = parsed.get("type")
                info = parsed.get("info", {})
                if instr_type == "transfer":
                    amt = info.get("amount")
                    src = info.get("source", "")
                    dst = info.get("destination", "")
                    mint = info.get("mint", "")
                    summary_lines.append(
                        f"Instruction {idx}: Transfer of {amt} tokens (mint {mint}) "
                        f"from {src[:4]}...{src[-4:]} to {dst[:4]}...{dst[-4:]}.")
                elif instr_type == "mintTo":
                    amt = info.get("amount")
                    mint = info.get("mint", "")
                    acct = info.get("account", "")
                    summary_lines.append(
                        f"Instruction {idx}: Minted {amt} new tokens of {mint} to {acct[:4]}...{acct[-4:]}.")
                else:
                    summary_lines.append(f"Instruction {idx}: SPL Token instruction **{instr_type}**.")
            elif program == "system" and "parsed" in instr:
                parsed = instr["parsed"]
                if parsed.get("type") == "transfer":
                    info = parsed.get("info", {})
                    lamports = info.get("lamports", 0)
                    src = info.get("source", "")
                    dst = info.get("destination", "")
                    sol_amount = int(lamports) / 1e9
                    summary_lines.append(
                        f"Instruction {idx}: SOL transfer of {sol_amount:.9f} SOL "
                        f"from {src[:4]}...{src[-4:]} to {dst[:4]}...{dst[-4:]}.")
                else:
                    summary_lines.append(f"Instruction {idx}: System program instruction **{parsed.get('type')}**.")
            else:
                # Unparsed or other program instructions
                prog_id = instr.get("programId") or instr.get("programIdIndex")
                summary_lines.append(f"Instruction {idx}: Instruction by program {prog_id} (details not parsed).")
    # Join all instruction summaries
    summary = "\n".join(summary_lines) if summary_lines else "No parsed instruction details available."
    return {"signature": signature, "summary": summary}

@app.get("/price/{symbol}")
def get_price(symbol: str):
    """
    Get current price, 24h change, volume (in SOL), and market cap for a given token symbol.
    Data is fetched from CoinGecko API.
    """
    query = symbol.strip().lower()
    # Search for the token on CoinGecko
    search_url = f"https://api.coingecko.com/api/v3/search?query={query}"
    try:
        sresp = requests.get(search_url, timeout=5)
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="CoinGecko search request failed")
    if sresp.status_code != 200:
        raise HTTPException(status_code=502, detail="CoinGecko search error")
    search_data = sresp.json()
    coin_id = None
    if search_data and "coins" in search_data:
        for coin in search_data["coins"]:
            if coin.get("symbol", "").lower() == query:
                coin_id = coin.get("id")
                break
        if not coin_id and search_data["coins"]:
            coin_id = search_data["coins"][0].get("id")
    if not coin_id:
        raise HTTPException(status_code=404, detail="Token not found on CoinGecko")
    # Get market data for the found coin (and Solana for volume conversion)
    ids_param = coin_id if coin_id == "solana" else f"{coin_id},solana"
    market_url = (
        "https://api.coingecko.com/api/v3/coins/markets"
        f"?vs_currency=usd&ids={ids_param}&price_change_percentage=24h"
    )
    try:
        mresp = requests.get(market_url, timeout=5)
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="CoinGecko market data request failed")
    if mresp.status_code != 200:
        raise HTTPException(status_code=502, detail="CoinGecko market data error")
    market_data = mresp.json()
    if not isinstance(market_data, list) or not market_data:
        raise HTTPException(status_code=502, detail="Invalid market data response")
    # Separate the target coin and Solana data
    coin_data = None
    sol_data = None
    for entry in market_data:
        if entry.get("id") == coin_id:
            coin_data = entry
        if entry.get("id") == "solana":
            sol_data = entry
    if not coin_data:
        raise HTTPException(status_code=502, detail="Coin data not found in response")
    # Use Solana's price to convert volume to SOL units
    sol_price = None
    if sol_data and "current_price" in sol_data:
        sol_price = sol_data["current_price"]
    else:
        # Fallback: fetch SOL price quickly if not included
        try:
            sol_simple = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd", timeout=3)
            if sol_simple.status_code == 200:
                sol_price = sol_simple.json().get("solana", {}).get("usd")
        except requests.RequestException:
            sol_price = None
    # Extract relevant fields for output
    symbol_out = coin_data.get("symbol", query).upper()
    price_usd = coin_data.get("current_price")
    change_pct = coin_data.get("price_change_percentage_24h")
    volume_usd = coin_data.get("total_volume")
    market_cap = coin_data.get("market_cap")
    if price_usd is None or change_pct is None or volume_usd is None or market_cap is None:
        raise HTTPException(status_code=502, detail="Incomplete data from CoinGecko")
    # Format the price and change
    price_str = (
        f"${price_usd:.2f}" if price_usd >= 1 
        else f"${price_usd:.4f}" if price_usd >= 0.1 
        else f"${price_usd:.6f}"
    )
    change_str = f"{change_pct:+.1f}%"
    # Format volume using SOL units if possible
    if sol_price and sol_price > 0:
        volume_sol = volume_usd / sol_price
        vol_str = format_amount(volume_sol) + " SOL"
    else:
        vol_str = format_amount(volume_usd) + " $"
    # Format market cap with suffixes
    if market_cap >= 1_000_000_000:
        mc_str = f"{market_cap/1_000_000_000:.1f} B"
    elif market_cap >= 1_000_000:
        mc_str = f"{market_cap/1_000_000:.1f} M"
    elif market_cap >= 1_000:
        mc_str = f"{market_cap/1_000:.1f} k"
    else:
        mc_str = str(int(market_cap))
    return {
        "symbol": symbol_out,
        "price": price_str,
        "change_24h": change_str,
        "volume": vol_str,
        "market_cap": mc_str
    }

def format_amount(n: float) -> str:
    """Helper to format large numbers with K/M/B suffixes."""
    try:
        value = float(n)
    except Exception:
        return "N/A"
    if value >= 1_000_000_000:
        return f"{value/1_000_000_000:.1f} B"
    elif value >= 1_000_000:
        return f"{value/1_000_000:.1f} M"
    elif value >= 1_000:
        return f"{value/1_000:.1f} k"
    else:
        if value >= 100:
            return f"{value:.0f}"
        elif value >= 1:
            return f"{value:.1f}"
        else:
            return f"{value:.2f}"

@app.get("/token")
def find_token(query: str):
    """Find a token by name or symbol and return its symbol, name, and Solana mint address."""
    q = query.strip()
    if not q:
        raise HTTPException(status_code=422, detail="Query parameter cannot be empty")
    # Use CoinGecko to search for the token by name or symbol
    search_url = f"https://api.coingecko.com/api/v3/search?query={q}"
    try:
        resp = requests.get(search_url, timeout=5)
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="CoinGecko search request failed")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="CoinGecko search error")
    data = resp.json()
    if not data or "coins" not in data or not data["coins"]:
        raise HTTPException(status_code=404, detail="Token not found")
    # Take the first matching result (prefer exact symbol match if possible)
    result = None
    for coin in data["coins"]:
        if coin.get("symbol", "").lower() == q.lower():
            result = coin
            break
    if result is None:
        result = data["coins"][0]
    coin_id = result.get("id")
    symbol = result.get("symbol", "").upper()
    name = result.get("name")
    if not coin_id:
        raise HTTPException(status_code=404, detail="Token not found on CoinGecko")
    # Get contract addresses from coin details (to find Solana address)
    detail_url = (
        f"https://api.coingecko.com/api/v3/coins/{coin_id}"
        "?localization=false&tickers=false&market_data=false"
        "&community_data=false&developer_data=false&sparkline=false"
    )
    try:
        dresp = requests.get(detail_url, timeout=5)
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="CoinGecko token detail request failed")
    if dresp.status_code != 200:
        raise HTTPException(status_code=502, detail="Error fetching token details")
    detail_data = dresp.json()
    platforms = detail_data.get("platforms", {})
    sol_mint = platforms.get("solana")
    if not sol_mint:
        # Token exists but not on Solana
        raise HTTPException(status_code=404, detail="Token not available on Solana")
    return {
        "symbol": symbol,
        "name": name,
        "mint": sol_mint
    }

@app.get("/mintinfo/{mint}")
def get_token_info_from_mint(mint: str):
    """Get token name and symbol from a given mint address using Jupiter and Helius."""
    name = None
    symbol = None
    # Try Jupiter's token info API
    try:
        jup_resp = requests.get(f"{JUPITER_TOKEN_INFO_URL}{mint}", timeout=5)
        if jup_resp.status_code == 200:
            jup_data = jup_resp.json()
            name = jup_data.get("name")
            symbol = jup_data.get("symbol")
    except Exception:
        pass
    # If Jupiter didn't have info, try Helius metadata
    if not name or not symbol:
        try:
            helius_meta = helius_token_metadata(mint)
            if helius_meta:
                if not name:
                    name = helius_meta.get("name")
                if not symbol:
                    symbol = helius_meta.get("symbol")
        except Exception:
            pass
    # Fallback names if still not found
    if not name:
        name = "Unlisted Token"
    if not symbol:
        symbol = mint[:4] + "..." + mint[-4:]
    # Check the owner program of the mint (to verify if it's a proper SPL token)
    owner = fetch_basic_token_info(mint)
    return {
        "mint": mint,
        "owner": owner or "Unknown",
        "name": name,
        "symbol": symbol
    }

@app.get("/pumpfun")
def get_latest_pumpfun_tokens():
    """List the latest Pump.fun coin launches (basic info for each)."""
    try:
        resp = requests.get(f"{PUMPFUN_API_BASE}/coins?limit=50", timeout=6)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Pump.fun API request failed: {e}")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Pump.fun API returned status {resp.status_code}")
    tokens = resp.json()
    if not isinstance(tokens, list):
        raise HTTPException(status_code=502, detail="Unexpected response format from Pump.fun API")
    # Format each token's info
    result_list = []
    for t in tokens:
        mint = t.get("metadata", {}).get("mint")
        if not mint:
            continue
        result_list.append({
            "name": t.get("name"),
            "symbol": t.get("symbol"),
            "mint": mint,
            "price": t.get("stats", {}).get("price"),
            "market_cap": t.get("stats", {}).get("marketCap"),
            "volume_24h": t.get("stats", {}).get("volume24h")
        })
    return result_list

@app.get("/pumpfun/{mint}")
def get_pumpfun_token_by_mint(mint: str):
    """Retrieve Pump.fun token info by its mint address, if it exists."""
    try:
        resp = requests.get(f"{PUMPFUN_API_BASE}/coins/{mint}", timeout=6)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Pump.fun API request failed: {e}")
    if resp.status_code == 404:
        # Coin not found on Pump.fun
        raise HTTPException(status_code=404, detail="Mint not found in Pump.fun listings")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Pump.fun API returned status {resp.status_code}")
    coin = resp.json()
    return {
        "name": coin.get("name"),
        "symbol": coin.get("symbol"),
        "mint": mint,
        "price": coin.get("stats", {}).get("price"),
        "market_cap": coin.get("stats", {}).get("marketCap"),
        "volume_24h": coin.get("stats", {}).get("volume24h")
    }

@app.get("/")
def root():
    return {"message": "SolanaGPT online — try /balances/{address} or /transaction/{signature}"}
