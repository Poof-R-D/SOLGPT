from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import requests
from decimal import Decimal
from functools import lru_cache
from random import shuffle

app = FastAPI(
    title="SolanaGPT",
    description="Poof Labs Solana degen trading assistant",
    version="1.0",
    servers=[{"url": "https://web-production-61325.up.railway.app"}]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

RPC_ENDPOINTS = [
    "https://rpc.helius.xyz/?api-key=a8b4eafc-da15-47d1-89a4-f3492ae7406a",
    "https://mainnet.helius-rpc.com",
    "https://api.mainnet-beta.solana.com",
    "https://solana-api.projectserum.com",
    "https://rpc.ankr.com/solana",
    "https://solana.rpcpool.com",
    "https://solana-mainnet.g.alchemy.com/v2/YOUR_API_KEY",
    "https://mainnet.rpc.solana.com",
    "https://solana-mainnet.rpc.extrnode.com",
    "https://api.metaplex.solana.com",
    "https://solana-api.syndica.io/access-token/YOUR_API_KEY/rpc"
]

JUPITER_TOKEN_INFO_URL = "https://tokens.jup.ag/token/"
JUPITER_PRICE_URL = "https://api.jup.ag/price/v2?ids="
JUPITER_TOKEN_LIST_URL = "https://token.jup.ag/all"
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

def fetch_basic_token_info(mint: str):
    """Fetch token account owner program (e.g. check if it's SPL)"""
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getAccountInfo",
        "params": [mint, {"encoding": "jsonParsed"}]
    }
    try:
        data = get_rpc_response(payload)
        owner = data.get("result", {}).get("value", {}).get("owner")
        return owner
    except:
        return None

def helius_token_metadata(mint: str):
    """Try to get richer metadata from Helius (if token is new or unknown)"""
    try:
        resp = requests.get(
            f"https://api.helius.xyz/v0/tokens/metadata?mint={mint}&api-key=cfa5bbd3-4880-4efe-87ac-319f7093cf7a", 
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and data:
                return data[0]  # get first result
        return None
    except:
        return None

@lru_cache(maxsize=1)
def get_jupiter_token_map():
    try:
        resp = requests.get(JUPITER_TOKEN_LIST_URL, timeout=10)
        tokens = resp.json()
        return {t['symbol'].lower(): t['address'] for t in tokens if 'symbol' in t and 'address' in t}
    except Exception:
        return {}

def get_token_mint_from_symbol(symbol: str) -> str:
    token_map = get_jupiter_token_map()
    mint = token_map.get(symbol.lower())
    if not mint:
        raise HTTPException(status_code=404, detail=f"Mint not found for symbol '{symbol}'")
    return mint

def resolve_to_mint(token_input: str) -> str:
    if len(token_input) > 30 and token_input.startswith("So"):
        return token_input
    return get_token_mint_from_symbol(token_input)

def get_rpc_response(payload):
    rpc_list = RPC_ENDPOINTS[:]
    shuffle(rpc_list)
    for url in rpc_list:
        try:
            resp = requests.post(url, json=payload, timeout=5)
            data = resp.json()
            if 'result' in data and data['result'] is not None:
                return data
        except Exception:
            continue
    raise Exception("All RPC endpoints failed or timed out")

def format_amount(n: float) -> str:
    """Format a number with K/M/B suffixes for readability."""
    if n is None:
        return "N/A"
    try:
        value = float(n)
    except:
        return "N/A"
    if value >= 1_000_000_000:
        return f"{value/1_000_000_000:.1f} B"
    elif value >= 1_000_000:
        return f"{value/1_000_000:.1f} M"
    elif value >= 1_000:
        return f"{value/1_000:.1f} k"
    else:
        # For values < 1000, use appropriate decimals
        if value >= 100:
            return f"{value:.0f}"
        elif value >= 1:
            return f"{value:.1f}"
        else:
            return f"{value:.2f}"

@app.get("/swap")
def simulate_swap(input_mint: str, output_mint: str, amount: float):
    """Simulate a token swap using Jupiter aggregator and return quote details."""
    # Prepare the raw amount for Jupiter (lamports for SOL, atomic units for others)
    input_mint_addr = input_mint
    output_mint_addr = output_mint
    if input_mint_addr == "So11111111111111111111111111111111111111112":
        # Convert SOL to lamports
        raw_amount = int(amount * 1e9)
    else:
        # For other tokens, assume amount is already in smallest units (atomic) 
        # (Better: fetch token decimals and convert)
        try:
            raw_amount = int(amount)
        except Exception:
            raw_amount = None
    if raw_amount is None:
        raise HTTPException(status_code=422, detail="Invalid amount")
    # Construct Jupiter quote API URL
    jup_url = (
        "https://lite-api.jup.ag/quote"
        f"?inputMint={input_mint_addr}&outputMint={output_mint_addr}"
        f"&amount={raw_amount}&slippageBps=50&restrictIntermediateTokens=true"
    )
    # Request quote from Jupiter
    try:
        jresp = requests.get(jup_url, timeout=5)
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="Jupiter quote request failed")
    if jresp.status_code != 200:
        raise HTTPException(status_code=502, detail="Jupiter API returned error")
    quote = jresp.json()
    if not quote or "outAmount" not in quote:
        raise HTTPException(status_code=502, detail="Invalid quote response")
    # Parse quote data
    in_amount_user = amount
    out_amount = int(quote["outAmount"])
    out_amount_str = f"{out_amount:,}"  # format with commas
    # Build route representation
    route_steps = []
    for step in quote.get("routePlan", []):
        swap_info = step.get("swapInfo", {})
        imint = swap_info.get("inputMint")
        omint = swap_info.get("outputMint")
        # Convert mint addresses to symbols or short tags
        def mint_to_symbol(mint_addr: str) -> str:
            known = {
                "So11111111111111111111111111111111111111112": "SOL",
                "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
                "Es9vMFrzaCjLwSX5ae4Ew9WVeEKXZotPwX3hPJJrEvDw": "USDT"
            }
            if mint_addr in known:
                return known[mint_addr]
            return f"{mint_addr[:4]}…{mint_addr[-3:]}" if mint_addr else "UNK"
        symbol_in = mint_to_symbol(imint)
        route_steps.append(symbol_in)
    if route_steps:
        # Append final output token symbol
        route_steps.append(mint_to_symbol(output_mint_addr))
    route_str = " -> ".join(route_steps)
    slippage_pct = 0.5  # 50 bps = 0.5%
    return {
        "input_amount": f"{in_amount_user} {route_steps[0]}",
        "output_estimate": f"{out_amount_str} {route_steps[-1]}",
        "slippage": f"{slippage_pct}%",
        "route": route_str,
        "platform": "Jupiter Aggregator"
    }

@app.get("/resolve")
def resolve_symbol(symbol: str):
    """Resolve a token symbol to its Solana mint address using Jupiter."""
    try:
        mint = resolve_to_mint(symbol)
        return {"symbol": symbol.upper(), "mint": mint}
    except HTTPException as e:
        raise e


@app.get("/balances/{address}")
def get_balances(address: str):
    result = {"sol": None, "tokens": []}

    balance_payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getBalance",
        "params": [address]
    }
    try:
        balance_data = get_rpc_response(balance_payload)
    except Exception as e:
        return {"error": "Unable to fetch SOL balance from RPC", "details": str(e)}
    lamports = balance_data.get("result", {}).get("value", 0)
    sol_amount = lamports / 1e9

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
        result["sol"] = {"amount": sol_amount, "price": None, "usd_value": None}
        result["tokens"] = []
        result["error"] = "Token account lookup failed"
        return result

    accounts = token_data.get("result", {}).get("value", [])
    token_list = []
    for acct in accounts:
        info = acct.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
        token_amount = info.get("tokenAmount", {})
        amount = None
        if token_amount.get("uiAmountString"):
            amount = Decimal(token_amount["uiAmountString"])
        elif token_amount.get("uiAmount") is not None:
            amount = Decimal(str(token_amount["uiAmount"]))
        else:
            try:
                raw = Decimal(token_amount.get("amount", "0"))
                decimals = int(token_amount.get("decimals", 0))
                amount = raw / (Decimal(10) ** decimals)
            except Exception:
                amount = Decimal(0)
        if amount is None or amount == 0:
            continue
        token_list.append({
            "mint": info.get("mint"),
            "amount": amount
        })

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

    tokens_output = []
    for token in token_list:
        mint = token["mint"]
        amount = token["amount"]
        name = "Unknown Token"
        symbol = mint[:4] + "..." + mint[-4:]
        volume = None

        try:
            meta_resp = requests.get(f"{JUPITER_TOKEN_INFO_URL}{mint}", timeout=5)
            if meta_resp.status_code == 200:
                meta = meta_resp.json()
                name = meta.get("name") or name
                symbol = meta.get("symbol") or symbol
                volume = meta.get("daily_volume")
        except Exception:
            pass

        price = None
        usd_value = None
        if mint in prices:
            price_str = prices[mint].get("price")
            if price_str is not None:
                try:
                    price = float(price_str)
                except:
                    price = float(Decimal(price_str))
        if price is not None:
            usd_value = float(Decimal(str(price)) * amount)

        tokens_output.append({
            "mint": mint,
            "name": name,
            "symbol": symbol,
            "amount": float(amount),
            "price": price,
            "usd_value": usd_value,
            "daily_volume": volume
        })

    sol_price = None
    sol_usd_value = None
    if WSOL_MINT in prices:
        price_str = prices[WSOL_MINT].get("price")
        if price_str is not None:
            try:
                sol_price = float(price_str)
            except:
                sol_price = float(Decimal(price_str))
    if sol_price is not None:
        sol_usd_value = sol_price * sol_amount

    result["sol"] = {
        "amount": sol_amount,
        "price": sol_price,
        "usd_value": sol_usd_value
    }
    result["tokens"] = tokens_output
    return result


@app.get("/transaction/{signature}")
def get_transaction(signature: str):
    tx_payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getTransaction",
        "params": [signature, {"encoding": "jsonParsed"}]
    }
    try:
        tx_data = get_rpc_response(tx_payload)
    except Exception as e:
        return {"error": "Unable to fetch transaction", "details": str(e)}
    if tx_data.get("result") is None:
        return {"error": "Transaction not found"}

    tx = tx_data["result"]
    summary_lines = []
    if "meta" in tx:
        if tx["meta"].get("err"):
            summary_lines.append("**Transaction Status**: Failed")
        else:
            summary_lines.append("**Transaction Status**: Success")
        fee = tx["meta"].get("fee")
        if fee is not None:
            summary_lines.append(f"**Fee Paid**: {fee} lamports")

    if "transaction" in tx and "message" in tx["transaction"]:
        instructions = tx["transaction"]["message"].get("instructions", [])
        for idx, instr in enumerate(instructions, start=1):
            if instr.get("program") == "spl-token" and "parsed" in instr:
                parsed = instr["parsed"]
                instr_type = parsed.get("type")
                info = parsed.get("info", {})
                if instr_type == "transfer":
                    amt = info.get("amount")
                    src = info.get("source", "")
                    dst = info.get("destination", "")
                    mint = info.get("mint", "")
                    summary_lines.append(f"Instruction {idx}: Transfer of {amt} tokens (mint {mint}) from {src[:4]}...{src[-4:]} to {dst[:4]}...{dst[-4:]}.")
                elif instr_type == "mintTo":
                    amt = info.get("amount")
                    mint = info.get("mint", "")
                    acct = info.get("account", "")
                    summary_lines.append(f"Instruction {idx}: Minted {amt} new tokens of {mint} to {acct[:4]}...{acct[-4:]}.")
                else:
                    summary_lines.append(f"Instruction {idx}: SPL token instruction **{instr_type}**.")
            elif instr.get("program") == "system" and "parsed" in instr:
                parsed = instr["parsed"]
                if parsed.get("type") == "transfer":
                    info = parsed.get("info", {})
                    lamports = info.get("lamports", 0)
                    src = info.get("source", "")
                    dst = info.get("destination", "")
                    sol_amount = int(lamports) / 1e9
                    summary_lines.append(f"Instruction {idx}: SOL transfer of {sol_amount:.9f} SOL from {src[:4]}...{src[-4:]} to {dst[:4]}...{dst[-4:]}.")
                else:
                    summary_lines.append(f"Instruction {idx}: System program instruction **{parsed.get('type')}**.")
            else:
                prog_id = instr.get("programId") or instr.get("programIdIndex")
                summary_lines.append(f"Instruction {idx}: Instruction by program {prog_id} (details not parsed).")

    summary = "\n".join(summary_lines) if summary_lines else "No parsed instruction details available."
    return {"signature": signature, "summary": summary}

@app.get("/price/{symbol}")
def get_price(symbol: str):
    """Get current price, 24h change, volume (in SOL), and market cap for a token symbol."""
    symbol_query = symbol.strip().lower()
    # Find coin ID by symbol/name using CoinGecko
    search_url = f"https://api.coingecko.com/api/v3/search?query={symbol_query}"
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
            if coin.get("symbol", "").lower() == symbol_query:
                coin_id = coin.get("id")
                break
        if not coin_id and search_data["coins"]:
            coin_id = search_data["coins"][0].get("id")
    if not coin_id:
        raise HTTPException(status_code=404, detail="Token not found on CoinGecko")
    # Fetch market data for the coin and for Solana (to convert volume to SOL)
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
    if not isinstance(market_data, list) or len(market_data) == 0:
        raise HTTPException(status_code=502, detail="Invalid market data response")
    # Parse coin and solana data
    coin_data = None
    sol_data = None
    for entry in market_data:
        if entry.get("id") == coin_id:
            coin_data = entry
        if entry.get("id") == "solana":
            sol_data = entry
    if not coin_data:
        raise HTTPException(status_code=502, detail="Coin data not found in response")
    # Get Solana price (for volume conversion to SOL)
    sol_price = sol_data["current_price"] if (sol_data and "current_price" in sol_data) else None
    if sol_price is None:
        try:
            sol_resp = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd", timeout=3)
            if sol_resp.status_code == 200:
                sol_price = sol_resp.json().get("solana", {}).get("usd")
        except requests.RequestException:
            sol_price = None
    # Extract and format required fields
    symbol_out = coin_data.get("symbol", symbol_query).upper()
    price_usd = coin_data.get("current_price")
    change_pct = coin_data.get("price_change_percentage_24h")
    volume_usd = coin_data.get("total_volume")
    market_cap = coin_data.get("market_cap")
    if price_usd is None or change_pct is None or volume_usd is None or market_cap is None:
        raise HTTPException(status_code=502, detail="Incomplete data from CoinGecko")
    # Format price and change
    price_str = (
        f"${price_usd:.2f}" if price_usd >= 1 
        else f"${price_usd:.4f}" if price_usd >= 0.1 
        else f"${price_usd:.6f}"
    )
    change_str = f"{change_pct:+.1f}%"
    # Format volume (convert USD to SOL volume if possible)
    if sol_price and sol_price > 0:
        vol_sol = volume_usd / sol_price
        vol_str = format_amount(vol_sol) + " SOL"
    else:
        vol_str = format_amount(volume_usd) + " $"  # fallback to USD volume
    # Format market cap
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
@app.get("/token")
def find_token(query: str):
    """Find a token by name or symbol and return its symbol, name, and Solana mint address."""
    q = query.strip()
    if not q:
        raise HTTPException(status_code=422, detail="Query parameter cannot be empty")
    # Use CoinGecko search to find the token
    url = f"https://api.coingecko.com/api/v3/search?query={q}"
    try:
        resp = requests.get(url, timeout=5)
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="CoinGecko search request failed")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="CoinGecko search error")
    data = resp.json()
    if not data or "coins" not in data or not data["coins"]:
        raise HTTPException(status_code=404, detail="Token not found")
    # Take the top search result for simplicity (or the first exact symbol match)
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
        raise HTTPException(status_code=404, detail="Token not found")
    # Fetch coin details to get contract addresses (mint) on Solana
    detail_url = f"https://api.coingecko.com/api/v3/coins/{coin_id}?localization=false&tickers=false&market_data=false&community_data=false&developer_data=false&sparkline=false"
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
        # If the token is not on Solana (no Solana mint address), return not found
        raise HTTPException(status_code=404, detail="Token not available on Solana")
    return {
        "symbol": symbol,
        "name": name,
        "mint": sol_mint
    }
    
@app.get("/mintinfo/{mint}")
def get_token_info_from_mint(mint: str):
    """Get token name/symbol from any source: Jupiter, Helius, or fallback"""
    name, symbol = None, None

    # 1. Try Jupiter token info
    try:
        jup_resp = requests.get(f"{JUPITER_TOKEN_INFO_URL}{mint}", timeout=5)
        if jup_resp.status_code == 200:
            jup_data = jup_resp.json()
            name = jup_data.get("name")
            symbol = jup_data.get("symbol")
    except:
        pass

    # 2. Try Helius metadata
    if not name or not symbol:
        try:
            helius = helius_token_metadata(mint)
            if helius:
                name = name or helius.get("name")
                symbol = symbol or helius.get("symbol")
        except:
            pass

    # 3. Final fallback if totally unknown
    if not name:
        name = "Unlisted Token"
    if not symbol:
        symbol = mint[:4] + "..." + mint[-4:]

    # 4. Try get owner from chain (basic validity check)
    owner = fetch_basic_token_info(mint)

    return {
        "mint": mint,
        "owner": owner or "Unknown",
        "name": name,
        "symbol": symbol
    }
def fetch_pumpfun_tokens(limit=50):
    """Fetch latest tokens from Pump.fun's GraphQL endpoint"""
    query = {
        "operationName": "Feed",
        "variables": {"sortBy": "new", "limit": limit},
        "query": """
            query Feed($sortBy: String, $limit: Int) {
              feed(sortBy: $sortBy, limit: $limit) {
                id
                name
                symbol
                createdAt
                metadata {
                  mint
                  imageUri
                }
                stats {
                  price
                  marketCap
                  volume24h
                }
              }
            }
        """
    }
    try:
        resp = requests.post(PUMPFUN_GRAPHQL_URL, json=query, timeout=6)
        if resp.status_code == 200:
            return resp.json().get("data", {}).get("feed", [])
        return []
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import requests
from decimal import Decimal
from functools import lru_cache
from random import shuffle

app = FastAPI(
    title="SolanaGPT",
    description="Poof Labs Solana degen trading assistant",
    version="1.0",
    servers=[{"url": "https://web-production-61325.up.railway.app"}]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

RPC_ENDPOINTS = [
    "https://rpc.helius.xyz/?api-key=a8b4eafc-da15-47d1-89a4-f3492ae7406a",
    "https://mainnet.helius-rpc.com",
    "https://api.mainnet-beta.solana.com",
    "https://solana-api.projectserum.com",
    "https://rpc.ankr.com/solana",
    "https://solana.rpcpool.com",
    "https://solana-mainnet.g.alchemy.com/v2/YOUR_API_KEY",
    "https://mainnet.rpc.solana.com",
    "https://solana-mainnet.rpc.extrnode.com",
    "https://api.metaplex.solana.com",
    "https://solana-api.syndica.io/access-token/YOUR_API_KEY/rpc"
]

JUPITER_TOKEN_INFO_URL = "https://tokens.jup.ag/token/"
JUPITER_PRICE_URL = "https://api.jup.ag/price/v2?ids="
JUPITER_TOKEN_LIST_URL = "https://token.jup.ag/all"
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

def fetch_basic_token_info(mint: str):
    """Fetch token account owner program (e.g. check if it's SPL)"""
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getAccountInfo",
        "params": [mint, {"encoding": "jsonParsed"}]
    }
    try:
        data = get_rpc_response(payload)
        owner = data.get("result", {}).get("value", {}).get("owner")
        return owner
    except:
        return None

def helius_token_metadata(mint: str):
    """Try to get richer metadata from Helius (if token is new or unknown)"""
    try:
        resp = requests.get(
            f"https://api.helius.xyz/v0/tokens/metadata?mint={mint}&api-key=cfa5bbd3-4880-4efe-87ac-319f7093cf7a", 
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and data:
                return data[0]  # get first result
        return None
    except:
        return None

@lru_cache(maxsize=1)
def get_jupiter_token_map():
    try:
        resp = requests.get(JUPITER_TOKEN_LIST_URL, timeout=10)
        tokens = resp.json()
        return {t['symbol'].lower(): t['address'] for t in tokens if 'symbol' in t and 'address' in t}
    except Exception:
        return {}

def get_token_mint_from_symbol(symbol: str) -> str:
    token_map = get_jupiter_token_map()
    mint = token_map.get(symbol.lower())
    if not mint:
        raise HTTPException(status_code=404, detail=f"Mint not found for symbol '{symbol}'")
    return mint

def resolve_to_mint(token_input: str) -> str:
    if len(token_input) > 30 and token_input.startswith("So"):
        return token_input
    return get_token_mint_from_symbol(token_input)

def get_rpc_response(payload):
    rpc_list = RPC_ENDPOINTS[:]
    shuffle(rpc_list)
    for url in rpc_list:
        try:
            resp = requests.post(url, json=payload, timeout=5)
            data = resp.json()
            if 'result' in data and data['result'] is not None:
                return data
        except Exception:
            continue
    raise Exception("All RPC endpoints failed or timed out")

def format_amount(n: float) -> str:
    """Format a number with K/M/B suffixes for readability."""
    if n is None:
        return "N/A"
    try:
        value = float(n)
    except:
        return "N/A"
    if value >= 1_000_000_000:
        return f"{value/1_000_000_000:.1f} B"
    elif value >= 1_000_000:
        return f"{value/1_000_000:.1f} M"
    elif value >= 1_000:
        return f"{value/1_000:.1f} k"
    else:
        # For values < 1000, use appropriate decimals
        if value >= 100:
            return f"{value:.0f}"
        elif value >= 1:
            return f"{value:.1f}"
        else:
            return f"{value:.2f}"

@app.get("/swap")
def simulate_swap(input_mint: str, output_mint: str, amount: float):
    """Simulate a token swap using Jupiter aggregator and return quote details."""
    # Prepare the raw amount for Jupiter (lamports for SOL, atomic units for others)
    input_mint_addr = input_mint
    output_mint_addr = output_mint
    if input_mint_addr == "So11111111111111111111111111111111111111112":
        # Convert SOL to lamports
        raw_amount = int(amount * 1e9)
    else:
        # For other tokens, assume amount is already in smallest units (atomic) 
        # (Better: fetch token decimals and convert)
        try:
            raw_amount = int(amount)
        except Exception:
            raw_amount = None
    if raw_amount is None:
        raise HTTPException(status_code=422, detail="Invalid amount")
    # Construct Jupiter quote API URL
    jup_url = (
        "https://lite-api.jup.ag/quote"
        f"?inputMint={input_mint_addr}&outputMint={output_mint_addr}"
        f"&amount={raw_amount}&slippageBps=50&restrictIntermediateTokens=true"
    )
    # Request quote from Jupiter
    try:
        jresp = requests.get(jup_url, timeout=5)
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="Jupiter quote request failed")
    if jresp.status_code != 200:
        raise HTTPException(status_code=502, detail="Jupiter API returned error")
    quote = jresp.json()
    if not quote or "outAmount" not in quote:
        raise HTTPException(status_code=502, detail="Invalid quote response")
    # Parse quote data
    in_amount_user = amount
    out_amount = int(quote["outAmount"])
    out_amount_str = f"{out_amount:,}"  # format with commas
    # Build route representation
    route_steps = []
    for step in quote.get("routePlan", []):
        swap_info = step.get("swapInfo", {})
        imint = swap_info.get("inputMint")
        omint = swap_info.get("outputMint")
        # Convert mint addresses to symbols or short tags
        def mint_to_symbol(mint_addr: str) -> str:
            known = {
                "So11111111111111111111111111111111111111112": "SOL",
                "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
                "Es9vMFrzaCjLwSX5ae4Ew9WVeEKXZotPwX3hPJJrEvDw": "USDT"
            }
            if mint_addr in known:
                return known[mint_addr]
            return f"{mint_addr[:4]}…{mint_addr[-3:]}" if mint_addr else "UNK"
        symbol_in = mint_to_symbol(imint)
        route_steps.append(symbol_in)
    if route_steps:
        # Append final output token symbol
        route_steps.append(mint_to_symbol(output_mint_addr))
    route_str = " -> ".join(route_steps)
    slippage_pct = 0.5  # 50 bps = 0.5%
    return {
        "input_amount": f"{in_amount_user} {route_steps[0]}",
        "output_estimate": f"{out_amount_str} {route_steps[-1]}",
        "slippage": f"{slippage_pct}%",
        "route": route_str,
        "platform": "Jupiter Aggregator"
    }

@app.get("/resolve")
def resolve_symbol(symbol: str):
    """Resolve a token symbol to its Solana mint address using Jupiter."""
    try:
        mint = resolve_to_mint(symbol)
        return {"symbol": symbol.upper(), "mint": mint}
    except HTTPException as e:
        raise e


@app.get("/balances/{address}")
def get_balances(address: str):
    result = {"sol": None, "tokens": []}

    balance_payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getBalance",
        "params": [address]
    }
    try:
        balance_data = get_rpc_response(balance_payload)
    except Exception as e:
        return {"error": "Unable to fetch SOL balance from RPC", "details": str(e)}
    lamports = balance_data.get("result", {}).get("value", 0)
    sol_amount = lamports / 1e9

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
        result["sol"] = {"amount": sol_amount, "price": None, "usd_value": None}
        result["tokens"] = []
        result["error"] = "Token account lookup failed"
        return result

    accounts = token_data.get("result", {}).get("value", [])
    token_list = []
    for acct in accounts:
        info = acct.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
        token_amount = info.get("tokenAmount", {})
        amount = None
        if token_amount.get("uiAmountString"):
            amount = Decimal(token_amount["uiAmountString"])
        elif token_amount.get("uiAmount") is not None:
            amount = Decimal(str(token_amount["uiAmount"]))
        else:
            try:
                raw = Decimal(token_amount.get("amount", "0"))
                decimals = int(token_amount.get("decimals", 0))
                amount = raw / (Decimal(10) ** decimals)
            except Exception:
                amount = Decimal(0)
        if amount is None or amount == 0:
            continue
        token_list.append({
            "mint": info.get("mint"),
            "amount": amount
        })

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

    tokens_output = []
    for token in token_list:
        mint = token["mint"]
        amount = token["amount"]
        name = "Unknown Token"
        symbol = mint[:4] + "..." + mint[-4:]
        volume = None

        try:
            meta_resp = requests.get(f"{JUPITER_TOKEN_INFO_URL}{mint}", timeout=5)
            if meta_resp.status_code == 200:
                meta = meta_resp.json()
                name = meta.get("name") or name
                symbol = meta.get("symbol") or symbol
                volume = meta.get("daily_volume")
        except Exception:
            pass

        price = None
        usd_value = None
        if mint in prices:
            price_str = prices[mint].get("price")
            if price_str is not None:
                try:
                    price = float(price_str)
                except:
                    price = float(Decimal(price_str))
        if price is not None:
            usd_value = float(Decimal(str(price)) * amount)

        tokens_output.append({
            "mint": mint,
            "name": name,
            "symbol": symbol,
            "amount": float(amount),
            "price": price,
            "usd_value": usd_value,
            "daily_volume": volume
        })

    sol_price = None
    sol_usd_value = None
    if WSOL_MINT in prices:
        price_str = prices[WSOL_MINT].get("price")
        if price_str is not None:
            try:
                sol_price = float(price_str)
            except:
                sol_price = float(Decimal(price_str))
    if sol_price is not None:
        sol_usd_value = sol_price * sol_amount

    result["sol"] = {
        "amount": sol_amount,
        "price": sol_price,
        "usd_value": sol_usd_value
    }
    result["tokens"] = tokens_output
    return result


@app.get("/transaction/{signature}")
def get_transaction(signature: str):
    tx_payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getTransaction",
        "params": [signature, {"encoding": "jsonParsed"}]
    }
    try:
        tx_data = get_rpc_response(tx_payload)
    except Exception as e:
        return {"error": "Unable to fetch transaction", "details": str(e)}
    if tx_data.get("result") is None:
        return {"error": "Transaction not found"}

    tx = tx_data["result"]
    summary_lines = []
    if "meta" in tx:
        if tx["meta"].get("err"):
            summary_lines.append("**Transaction Status**: Failed")
        else:
            summary_lines.append("**Transaction Status**: Success")
        fee = tx["meta"].get("fee")
        if fee is not None:
            summary_lines.append(f"**Fee Paid**: {fee} lamports")

    if "transaction" in tx and "message" in tx["transaction"]:
        instructions = tx["transaction"]["message"].get("instructions", [])
        for idx, instr in enumerate(instructions, start=1):
            if instr.get("program") == "spl-token" and "parsed" in instr:
                parsed = instr["parsed"]
                instr_type = parsed.get("type")
                info = parsed.get("info", {})
                if instr_type == "transfer":
                    amt = info.get("amount")
                    src = info.get("source", "")
                    dst = info.get("destination", "")
                    mint = info.get("mint", "")
                    summary_lines.append(f"Instruction {idx}: Transfer of {amt} tokens (mint {mint}) from {src[:4]}...{src[-4:]} to {dst[:4]}...{dst[-4:]}.")
                elif instr_type == "mintTo":
                    amt = info.get("amount")
                    mint = info.get("mint", "")
                    acct = info.get("account", "")
                    summary_lines.append(f"Instruction {idx}: Minted {amt} new tokens of {mint} to {acct[:4]}...{acct[-4:]}.")
                else:
                    summary_lines.append(f"Instruction {idx}: SPL token instruction **{instr_type}**.")
            elif instr.get("program") == "system" and "parsed" in instr:
                parsed = instr["parsed"]
                if parsed.get("type") == "transfer":
                    info = parsed.get("info", {})
                    lamports = info.get("lamports", 0)
                    src = info.get("source", "")
                    dst = info.get("destination", "")
                    sol_amount = int(lamports) / 1e9
                    summary_lines.append(f"Instruction {idx}: SOL transfer of {sol_amount:.9f} SOL from {src[:4]}...{src[-4:]} to {dst[:4]}...{dst[-4:]}.")
                else:
                    summary_lines.append(f"Instruction {idx}: System program instruction **{parsed.get('type')}**.")
            else:
                prog_id = instr.get("programId") or instr.get("programIdIndex")
                summary_lines.append(f"Instruction {idx}: Instruction by program {prog_id} (details not parsed).")

    summary = "\n".join(summary_lines) if summary_lines else "No parsed instruction details available."
    return {"signature": signature, "summary": summary}

@app.get("/price/{symbol}")
def get_price(symbol: str):
    """Get current price, 24h change, volume (in SOL), and market cap for a token symbol."""
    symbol_query = symbol.strip().lower()
    # Find coin ID by symbol/name using CoinGecko
    search_url = f"https://api.coingecko.com/api/v3/search?query={symbol_query}"
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
            if coin.get("symbol", "").lower() == symbol_query:
                coin_id = coin.get("id")
                break
        if not coin_id and search_data["coins"]:
            coin_id = search_data["coins"][0].get("id")
    if not coin_id:
        raise HTTPException(status_code=404, detail="Token not found on CoinGecko")
    # Fetch market data for the coin and for Solana (to convert volume to SOL)
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
    if not isinstance(market_data, list) or len(market_data) == 0:
        raise HTTPException(status_code=502, detail="Invalid market data response")
    # Parse coin and solana data
    coin_data = None
    sol_data = None
    for entry in market_data:
        if entry.get("id") == coin_id:
            coin_data = entry
        if entry.get("id") == "solana":
            sol_data = entry
    if not coin_data:
        raise HTTPException(status_code=502, detail="Coin data not found in response")
    # Get Solana price (for volume conversion to SOL)
    sol_price = sol_data["current_price"] if (sol_data and "current_price" in sol_data) else None
    if sol_price is None:
        try:
            sol_resp = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd", timeout=3)
            if sol_resp.status_code == 200:
                sol_price = sol_resp.json().get("solana", {}).get("usd")
        except requests.RequestException:
            sol_price = None
    # Extract and format required fields
    symbol_out = coin_data.get("symbol", symbol_query).upper()
    price_usd = coin_data.get("current_price")
    change_pct = coin_data.get("price_change_percentage_24h")
    volume_usd = coin_data.get("total_volume")
    market_cap = coin_data.get("market_cap")
    if price_usd is None or change_pct is None or volume_usd is None or market_cap is None:
        raise HTTPException(status_code=502, detail="Incomplete data from CoinGecko")
    # Format price and change
    price_str = (
        f"${price_usd:.2f}" if price_usd >= 1 
        else f"${price_usd:.4f}" if price_usd >= 0.1 
        else f"${price_usd:.6f}"
    )
    change_str = f"{change_pct:+.1f}%"
    # Format volume (convert USD to SOL volume if possible)
    if sol_price and sol_price > 0:
        vol_sol = volume_usd / sol_price
        vol_str = format_amount(vol_sol) + " SOL"
    else:
        vol_str = format_amount(volume_usd) + " $"  # fallback to USD volume
    # Format market cap
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
@app.get("/token")
def find_token(query: str):
    """Find a token by name or symbol and return its symbol, name, and Solana mint address."""
    q = query.strip()
    if not q:
        raise HTTPException(status_code=422, detail="Query parameter cannot be empty")
    # Use CoinGecko search to find the token
    url = f"https://api.coingecko.com/api/v3/search?query={q}"
    try:
        resp = requests.get(url, timeout=5)
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="CoinGecko search request failed")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="CoinGecko search error")
    data = resp.json()
    if not data or "coins" not in data or not data["coins"]:
        raise HTTPException(status_code=404, detail="Token not found")
    # Take the top search result for simplicity (or the first exact symbol match)
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
        raise HTTPException(status_code=404, detail="Token not found")
    # Fetch coin details to get contract addresses (mint) on Solana
    detail_url = f"https://api.coingecko.com/api/v3/coins/{coin_id}?localization=false&tickers=false&market_data=false&community_data=false&developer_data=false&sparkline=false"
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
        # If the token is not on Solana (no Solana mint address), return not found
        raise HTTPException(status_code=404, detail="Token not available on Solana")
    return {
        "symbol": symbol,
        "name": name,
        "mint": sol_mint
    }
    
@app.get("/mintinfo/{mint}")
def get_token_info_from_mint(mint: str):
    """Get token name/symbol from any source: Jupiter, Helius, or fallback"""
    name, symbol = None, None

    # 1. Try Jupiter token info
    try:
        jup_resp = requests.get(f"{JUPITER_TOKEN_INFO_URL}{mint}", timeout=5)
        if jup_resp.status_code == 200:
            jup_data = jup_resp.json()
            name = jup_data.get("name")
            symbol = jup_data.get("symbol")
    except:
        pass

    # 2. Try Helius metadata
    if not name or not symbol:
        try:
            helius = helius_token_metadata(mint)
            if helius:
                name = name or helius.get("name")
                symbol = symbol or helius.get("symbol")
        except:
            pass

    # 3. Final fallback if totally unknown
    if not name:
        name = "Unlisted Token"
    if not symbol:
        symbol = mint[:4] + "..." + mint[-4:]

    # 4. Try get owner from chain (basic validity check)
    owner = fetch_basic_token_info(mint)

    return {
        "mint": mint,
        "owner": owner or "Unknown",
        "name": name,
        "symbol": symbol
    }
def fetch_pumpfun_tokens(limit=50):
    """Fetch latest tokens from Pump.fun's GraphQL endpoint"""
    query = {
        "operationName": "Feed",
        "variables": {"sortBy": "new", "limit": limit},
        "query": """
            query Feed($sortBy: String, $limit: Int) {
              feed(sortBy: $sortBy, limit: $limit) {
                id
                name
                symbol
                createdAt
                metadata {
                  mint
                  imageUri
                }
                stats {
                  price
                  marketCap
                  volume24h
                }
              }
            }
        """
    }
    try:
        resp = requests.post(PUMPFUN_GRAPHQL_URL, json=query, timeout=6)
        if resp.status_code == 200:
            return resp.json().get("data", {}).get("feed", [])
        return []
    except Exception:
        return []

@app.get("/pumpfun")
def get_latest_pumpfun_tokens():
    """List latest Pump.fun launches (basic info)"""
    tokens = fetch_pumpfun_tokens()
    return [
        {
            "name": t.get("name"),
            "symbol": t.get("symbol"),
            "mint": t.get("metadata", {}).get("mint"),
            "price": t.get("stats", {}).get("price"),
            "market_cap": t.get("stats", {}).get("marketCap"),
            "volume_24h": t.get("stats", {}).get("volume24h")
        }
        for t in tokens if t.get("metadata", {}).get("mint")
    ]

@app.get("/pumpfun/{mint}")
def get_pumpfun_token_by_mint(mint: str):
    """Check if a given mint matches a Pump.fun token"""
    tokens = fetch_pumpfun_tokens(200)  # search through more for better coverage
    for t in tokens:
        if t.get("metadata", {}).get("mint") == mint:
            return {
                "name": t.get("name"),
                "symbol": t.get("symbol"),
                "mint": mint,
                "price": t.get("stats", {}).get("price"),
                "market_cap": t.get("stats", {}).get("marketCap"),
                "volume_24h": t.get("stats", {}).get("volume24h")
            }
    raise HTTPException(status_code=404, detail="Mint not found in Pump.fun listings")

@app.get("/")
def root():
    return {"message": "SolanaGPT online — try /balances/{address} or /transaction/{signature}"}
