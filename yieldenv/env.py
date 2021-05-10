# make sure User is recognized
from __future__ import annotations
from dataclasses import dataclass, field
import numpy as np
import logging
from typing import Dict, Optional, cast


@dataclass
class Env:
    users: Dict[str, User] = field(default_factory=dict)
    prices: Dict[str, float] = field(
        default_factory=lambda: {"dai": 1.0, "eth": 2.0, "i-dai": 1.0, "b-dai": -1.0}
    )


class User:
    def __init__(self, env: Env, name: str, funds_available: Optional[dict] = None):
        assert name not in env.users, f"User {name} exists"
        self.env = env
        # add the user to the environment
        self.env.users[name] = self

        if funds_available is None:
            funds_available = {"dai": 0.0, "eth": 0.0}
        self.funds_available = funds_available
        self.env = env

        self.name = name

    @property
    def wealth(self) -> float:

        user_wealth = sum(
            value * self.env.prices[asset_name]
            for asset_name, value in self.funds_available.items()
        )
        logging.info(f"{self.name}'s wealth in DAI: {user_wealth}")

        return user_wealth

    # # AMM actions
    # def initiate_amm(
    #     self, reward_token_name, initial_reserves, fee, asset_names
    # ) -> CPAmm:

    #     CPAmm(
    #         env=self.env,
    #         reward_token_name=reward_token_name,
    #         initial_reserves=initial_reserves,
    #         fee=fee,
    #         asset_names=asset_names,
    #     )

    def sell_to_amm(self, amm: CPAmm, sell_quantity: float, sell_index: int = 0):
        """
        `quantity` means how much quantity to sell (add) to the pool;
        index means which asset it is
        """

        # initialize asset balance if not yet there
        for w in amm.asset_names:
            if w not in self.funds_available:
                self.funds_available[w] = 0.0

        assert sell_quantity >= 0, "must sell a non-negative quantity"
        assert sell_index in [0, 1], "reserve_index out of range"

        assert (
            self.funds_available[amm.asset_names[sell_index]] >= sell_quantity
        ), "insufficient funds to sell"

        old_invariant = amm.invariant

        # deduct sold quantity from user's funds
        self.funds_available[amm.asset_names[sell_index]] -= sell_quantity

        # add sold quantity to the reserve with `reserve_index`
        amm.reserves[sell_index] += sell_quantity
        amm.volumes[sell_index] += sell_quantity

        # theoretical new quantity of the other reserve
        _new_reserve = old_invariant / amm.reserves[sell_index]
        buy_index = 1 - sell_index
        # theoretical quantity purchased

        buy_quantity = amm.reserves[buy_index] - _new_reserve

        # keep a record of the volume on the buy side
        amm.volumes[buy_index] += buy_quantity

        # fee charge
        swap_fee = buy_quantity * amm.fee
        # add fee to revenue
        amm.fee_retained_by_reserve[buy_index] += swap_fee

        # actual quantity the trader gets
        buy_quantity -= swap_fee

        # add buy quantity to user's funds
        self.funds_available[amm.asset_names[buy_index]] += buy_quantity

        # update actual new quantity of the buy reserve
        amm.reserves[buy_index] -= buy_quantity

        # update market price due to trading
        self.env.prices[amm.asset_names[1]] = amm.spot_price

        logging.debug(
            f"""
            old invariant: {old_invariant}
            Sell {sell_quantity} in asset#{sell_index}, get {buy_quantity} in asset#{buy_index},
            Earned fee: {swap_fee} in asset#{buy_index}
            new invariant: {amm.invariant}
            """
        )

    def update_liquidity(self, pool_shares_delta: float, amm: CPAmm):

        """
        add (pool_shares_delta>0) or remove (pool_shares_delta<0) liquidity to an AMM
        """

        if self.name not in amm.user_pool_shares:
            amm.user_pool_shares[self.name] = 0

        assert (
            amm.user_pool_shares[self.name] + pool_shares_delta >= 0
        ), "cannot deplete user pool shares"

        # with signs, + means add to pool. - means remove from pool
        liquidity_fraction_delta = pool_shares_delta / amm.total_pool_shares
        funds_delta = [w * liquidity_fraction_delta for w in amm.reserves]

        assert all(
            self.funds_available[amm.asset_names[i]] - funds_delta[i] >= 0
            for i in range(2)
        ), "insufficient funds to provide liquidity"

        for i in range(2):
            # update own funds
            self.funds_available[amm.asset_names[i]] -= funds_delta[i]
            # update liquidity pool
            amm.reserves[i] += funds_delta[i]

        # update pool shares of the user in the pool registry
        amm.user_pool_shares[self.name] += pool_shares_delta

        # matching balance in user's account to pool registry record
        self.funds_available[amm.lp_token_name] = amm.user_pool_shares[self.name]

    # -------------------------  PLF actions ------------------------------

    def supply_withdraw(self, amount: float, plf: Plf):  # negative for withdrawing

        if self.name not in plf.user_i_tokens:
            plf.user_i_tokens[self.name] = 0

        assert (
            plf.user_i_tokens[self.name] + amount >= 0
        ), "cannot withdraw more i-tokens than you have"

        assert (
            self.funds_available[plf.asset_names] - amount >= 0
        ), "insufficient funds to provide liquidity"

        self.funds_available[plf.asset_names] -= amount

        # update liquidity pool
        plf.total_available_funds += amount

        # update i tokens of the user in the pool registry
        plf.user_i_tokens[self.name] += amount

        # matching balance in user's account to pool registry record
        self.funds_available[plf.interest_token_name] = plf.user_i_tokens[self.name]

    def borrow_repay(self, amount: float, plf: Plf):

        if self.name not in plf.user_b_tokens:
            plf.user_b_tokens[self.name] = 0

        assert (
            plf.user_b_tokens[self.name] + amount >= 0
        ), "cannot repay more b-tokens than you have"

        assert (
            self.funds_available[plf.interest_token_name]
            - amount * plf.collateral_ratio
            >= 0
        ), "insufficient i-tokens to get the amount of requested b-tokens"

        self.funds_available[plf.interest_token_name] -= amount

        # update liquidity pool
        plf.total_borrowed_funds += amount

        # update b tokens of the user in the pool registry
        plf.user_b_tokens[self.name] += amount

        # matching balance in user's account to pool registry record
        self.funds_available[plf.borrow_token_name] = plf.user_b_tokens[self.name]


@dataclass
class CPAmm:
    """
    reserves[0] is the numeraire
    fee represents the fraction of **output asset** that got retained within the pool
    """

    env: Env
    reward_token_name: str
    initiator: User
    initial_reserves: list[float] = field(default_factory=lambda: [50.0, 50.0])
    fee: float = 0.005
    asset_names: list[str] = field(default_factory=lambda: ["dai", "eth"])

    def __post_init__(self):
        lp_token_name = "-".join(self.asset_names) + "-lp"
        self.lp_token_name = lp_token_name

        available_prices = self.env.prices
        if lp_token_name in available_prices and available_prices[lp_token_name] != 0:
            raise RuntimeError(
                "an amm pool with the same token pair exists. cannot create new one."
            )

        assert all(
            w > 0 for w in self.initial_reserves
        ), "must provide positive liquidity on both sides"

        assert all(
            self.asset_names[i] in self.initiator.funds_available
            and self.initiator.funds_available[self.asset_names[i]]
            >= self.initial_reserves[i]
            for i in range(2)
        ), "insufficient funds"

        # deduct funds from user balance
        for i in range(2):
            self.initiator.funds_available[
                self.asset_names[i]
            ] -= self.initial_reserves[i]

        self.reserves = self.initial_reserves

        initial_pool_share = 1.0
        self.user_pool_shares = {self.initiator.name: initial_pool_share}

        # add LP token into initiator's wallet
        self.initiator.funds_available[lp_token_name] = initial_pool_share

        # update 'eth' market price as implied by pool composition
        available_prices[self.asset_names[1]] = self.spot_price

        # update LP token and reward token price immediately
        available_prices[lp_token_name] = self.lp_token_price

        # if reward token is a new token, then initiate price with 0
        reward_token_name = self.reward_token_name
        if reward_token_name not in available_prices:
            available_prices[self.reward_token_name] = 0

        # initalize volume and fee bookkeeping
        self.volumes = [0.0, 0.0]
        self.fee_retained_by_reserve = [0.0, 0.0]

    @property
    def total_pool_shares(self) -> float:
        return sum(self.user_pool_shares.values())

    @property
    def invariant(self) -> float:
        return cast(float, np.prod(self.reserves))

    @property
    def spot_price(self) -> float:
        return self.reserves[0] / self.reserves[1]

    @property
    def pool_value(self) -> float:
        return self.reserves[0] * 2

    @property
    def lp_token_price(self) -> float:
        if self.total_pool_shares == 0:
            return 0.0
        return self.pool_value / self.total_pool_shares

    # # this does not make sense any more if we allow liquidity addition
    # @property
    # def value_held(self):
    #     return self.initial_reserves[0] + self.initial_reserves[1] * self.spot_price

    def __repr__(self):
        return f"(reserves={self.reserves},invariant={self.invariant})"

    def get_user_pool_fraction(self, user_name: str) -> float:
        if user_name not in self.user_pool_shares:
            self.user_pool_shares[user_name] = 0.0
        return self.user_pool_shares[user_name] / self.total_pool_shares

    def distribute_reward(self, quantity: float):
        for user_name in self.user_pool_shares:
            user_funds = self.env.users[user_name].funds_available

            # initialize if the balance does not exist before airdropping
            if self.reward_token_name not in user_funds:
                user_funds[self.reward_token_name] = 0

            # distribute reward token proportionaly
            user_funds[self.reward_token_name] += (
                self.get_user_pool_fraction(user_name=user_name) * quantity
            )


@dataclass
class Plf:
    env: Env
    initiator: User
    reward_token_name: str = "aave"
    supply_apy: float = 0.06
    borrow_apy: float = 0.07
    initial_starting_funds: float = 1000
    collateral_ratio: float = 1.2
    asset_names: str = "dai"  # you can only deposit and borrow 1 token

    def __post_init__(self):
        self.total_available_funds = self.initial_starting_funds
        self.total_borrowed_funds = 0  # start with no funds borrowed

        available_prices = self.env.prices
        self.interest_token_name = "i-" + self.asset_names
        self.borrow_token_name = "b-" + self.asset_names

        # if (
        #     self.interest_token_name in available_prices
        #     and available_prices[self.interest_token_name] != 0
        # ):
        #     raise RuntimeError("the dai pool is already created.")

        assert (
            self.asset_names in self.initiator.funds_available
            and self.initiator.funds_available[self.asset_names]
            >= self.initial_starting_funds
        ), "insufficient funds"

        # deduct funds from user balance
        self.initiator.funds_available[self.asset_names] -= self.initial_starting_funds

        initial_i_tokens = self.initial_starting_funds
        self.user_i_tokens = {self.initiator.name: initial_i_tokens}

        initial_b_tokens = 0
        self.user_b_tokens = {self.initiator.name: initial_b_tokens}

        # add interest-bearing token into initiator's wallet
        self.initiator.funds_available[
            self.interest_token_name
        ] = self.initial_starting_funds
        self.initiator.funds_available[self.borrow_token_name] = 0

        # if reward token is a new token, then initiate price with 0
        reward_token_name = self.reward_token_name
        if reward_token_name not in available_prices:
            available_prices[self.reward_token_name] = 0

    def setSupplyApy(self, apy: float):
        self.supply_apy = apy

    def setBorrowApy(self, apy: float):
        self.borrow_apy = apy

    def __repr__(self):
        return f"(available funds = {self.total_available_funds}, borrowed funds = {self.total_borrowed_funds})"

    @property
    def total_pool_shares(self) -> float:
        total_i_tokens = sum(self.user_i_tokens.values())
        total_b_tokens = sum(self.user_b_tokens.values())
        return total_i_tokens, total_b_tokens

    def get_user_pool_fraction(self, user_name: str) -> float:
        if user_name not in self.user_i_tokens:
            self.user_i_tokens[user_name] = 0.0
        i_token_fraction = self.user_i_tokens[user_name] / self.total_pool_shares[0]

        if user_name not in self.user_b_tokens:
            self.user_b_tokens[user_name] = 0.0
        b_token_fraction = self.user_b_tokens[user_name] / self.total_pool_shares[1]

        return i_token_fraction, b_token_fraction

    def receive_pay_interest(self):
        for user_name in self.user_i_tokens:
            user_funds = self.env.users[user_name].funds_available

            # distribute reward token proportionaly
            user_funds[self.interest_token_name] += user_funds[
                self.interest_token_name
            ] * ((1 + self.supply_apy) ** (1 / 365) - 1)

        for user_name in self.user_b_tokens:
            user_funds = self.env.users[user_name].funds_available

            if self.borrow_token_name not in user_funds:
                user_funds[self.borrow_token_name] = 0

            # distribute reward token proportionaly
            user_funds[self.borrow_token_name] += user_funds[self.borrow_token_name] * (
                (1 + self.borrow_apy) ** (1 / 365) - 1
            )

    def distribute_reward(self, quantity: float):
        for user_name in self.user_i_tokens:
            user_funds = self.env.users[user_name].funds_available

            # initialize if the balance does not exist before airdropping
            if self.reward_token_name not in user_funds:
                user_funds[self.reward_token_name] = 0

            # distribute reward token proportionaly
            user_funds[self.reward_token_name] += (
                self.get_user_pool_fraction(user_name=user_name)[0]
                * quantity
                * 0.5  # 50% of rewards go to suppliers
            )

        for user_name in self.user_b_tokens:
            user_funds = self.env.users[user_name].funds_available

            # initialize if the balance does not exist before airdropping
            if self.reward_token_name not in user_funds:
                user_funds[self.reward_token_name] = 0

            # distribute reward token proportionaly
            user_funds[self.reward_token_name] += (
                self.get_user_pool_fraction(user_name=user_name)[1]
                * quantity
                * 0.5  # 50% of rewards go to borrowers
            )