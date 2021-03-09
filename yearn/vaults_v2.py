import time
from dataclasses import dataclass
from threading import Thread
from typing import List

from brownie import chain, web3
from brownie.network.contract import Contract, InterfaceContainer

from yearn import strategies, uniswap
from yearn.events import UnknownEvent, decode_logs, fetch_events, get_logs
from yearn.mutlicall import fetch_multicall

VAULT_VIEWS_SCALED = [
    "totalAssets",
    "maxAvailableShares",
    "pricePerShare",
    "debtOutstanding",
    "creditAvailable",
    "expectedReturn",
    "totalSupply",
    "availableDepositLimit",
    "depositLimit",
    "totalDebt",
    "debtLimit",
]


@dataclass
class VaultV2:
    name: str
    api_version: str
    vault: InterfaceContainer
    strategies: List[strategies.Strategy]

    def __post_init__(self):
        # mutlicall-safe views with 0 inputs and numeric output.
        self._views = [
            x["name"]
            for x in self.vault.abi
            if x["type"] == "function"
            and x["stateMutability"] == "view"
            and not x["inputs"]
            and x["outputs"][0]["type"] == "uint256"
        ]

    def describe(self):
        scale = 10 ** self.vault.decimals()
        try:
            results = fetch_multicall(*[[self.vault, view] for view in self._views])
            info = dict(zip(self._views, results))
            for name in info:
                if name in VAULT_VIEWS_SCALED:
                    info[name] /= scale
            info["strategies"] = {}
        except ValueError as e:
            info = {"strategies": {}}
        for strat in self.strategies:
            info["strategies"][strat.name] = strat.describe()

        info["token price"] = uniswap.token_price(self.vault.token())
        if "totalAssets" in info:
            info["tvl"] = info["token price"] * info["totalAssets"]

        return info


def get_vaults(event_key="NewVault"):
    registry = Contract("v2.registry.ychad.eth")
    events = fetch_events(registry)
    versions = {x["api_version"]: Contract(x["template"]).abi for x in events["NewRelease"]}
    vaults = [
        Contract.from_abi(f'Vault v{vault["api_version"]}', vault["vault"], versions[vault["api_version"]])
        for vault in events[event_key]
    ]
    symbols = fetch_multicall(*[[x, "symbol"] for x in vaults])
    names = [f'{name} {vault["api_version"]}' for vault, name in zip(events[event_key], symbols)]
    return [
        VaultV2(name=name, api_version=event["api_version"], vault=vault, strategies=[])
        for name, vault, event in zip(names, vaults, events[event_key])
    ]


def get_experimental_vaults():
    return get_vaults("NewExperimentalVault")


class Registry:

    def __init__(self):
        start = time.time()
        print('Loading Vaults v2 Registry...')
        self.registry = Contract(web3.ens.resolve("v2.registry.ychad.eth"))
        {'deployed_block', 'experimental_block', 'endorsed_block'}
        self.governance = None
        self.api_versions = {}
        self.vaults = {}
        self.names = {}
        self.endorsed = {}
        self.tags = {}
        self.events = fetch_events(self.registry)
        self.process_events(self.events)
        self.last_block = self.events[-1].block_number
        self.thread = Thread(target=self.watch_events)
        self.thread.start()
        elapsed = time.time() - start
        print(f'Loaded {len(self.api_versions)} releases and {len(self.vaults)} vaults in {elapsed:.2f}s')

    def process_events(self, events):
        for evt in events:
            if evt.name == 'NewGovernance':
                self.governance = evt['governance']

            elif evt.name == 'NewRelease':
                self.api_versions[evt['api_version']] = Contract(evt['template'])

            elif evt.name in ['NewVault', 'NewExperimentalVault']:
                if evt['vault'] in self.vaults:
                    continue
                vault = Contract.from_abi(
                    f'Vault v{evt["api_version"]}',
                    evt['vault'],
                    self.api_versions[evt['api_version']].abi
                )
                self.vaults[evt['vault']] = vault
                self.names[evt['vault']] = f'{vault.symbol()} {evt["api_version"]}'

            elif evt.name == 'VaultTagged':
                self.tags[evt['vault']] = evt['tag']
            
            else:
                raise UnknownEvent(evt.name)

    def watch_events(self):
        for block in chain.new_blocks(height_buffer=10):
            logs = get_logs(str(self.registry), self.last_block + 1, block.number)
            self.process_events(decode_logs(logs))
            self.last_block = block.number
