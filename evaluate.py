import argparse
import math
import json
from typing import List
from datetime import datetime, timedelta
import typedload
from models import (
    Currency,
    Shares,
    Ratio,
    Stock,
    Valuation,
    IncomeStatement,
    BalanceSheet,
    CashFlowStatement,
    ValuationModel,
    Symbol,
)
import utils
from getStockSnapshot import getStockSnapshot, getHistoricalPrice
from getStocks import getStockList
from decimal import Decimal


def customRound(number, digits):
    rounded = round(number, digits)

    return rounded


def getDividendYield(
    dividendsPaid: Currency, sharesOutstanding: Shares, currentPrice: Currency
) -> Ratio:
    return utils.safeDivide(
        (utils.safeDivide(dividendsPaid, sharesOutstanding)), currentPrice
    )


def getMarketCap(sharesOutstanding: Shares, currentPrice: Currency) -> Currency:
    return sharesOutstanding * currentPrice


def getEquity(assets: Currency, liabilities: Currency) -> Currency:
    return assets - liabilities


def getRoe(netIncome: Currency, equity: Currency) -> Ratio:
    return utils.safeDivide(netIncome, equity)


def getRoa(netIncome: Currency, assets: Currency) -> Ratio:
    return utils.safeDivide(netIncome, assets)


def getDte(totalDebt: Currency, equity: Currency) -> Ratio:
    return utils.safeDivide(totalDebt, equity)


def getCr(currentAssets: Currency, currentLiabilities: Currency) -> Ratio:
    return utils.safeDivide(currentAssets, currentLiabilities)


def getFcf(operatingCashFlow: Currency, capex: Currency) -> Currency:
    return operatingCashFlow - abs(capex)


def getEps(netIncome: Currency, sharesOutstanding: Shares) -> Ratio:
    return utils.safeDivide(netIncome, sharesOutstanding)


def getPe(currentPrice: Currency, eps: Ratio) -> Ratio:
    return utils.safeDivide(currentPrice, eps)


def getPeg(pe: Ratio, growthRate: Ratio) -> Ratio:
    if not growthRate:
        return 0

    peg = pe / (100 * growthRate)

    isNegative = pe < 0 and growthRate < 0
    if isNegative:
        return peg * -1

    return peg


def getPb(currentPrice: Currency, equity: Currency, sharesOutstanding: Shares) -> Ratio:
    return utils.safeDivide(currentPrice, utils.safeDivide(equity, sharesOutstanding))


def getGrowthRate(values):
    # if we only have one value, we can't calculate growthRate
    # in this case rather return 0
    if len(values) <= 1:
        return 0

    finalValue = values[len(values) - 1]
    initialValue = values[0]
    noOfValues = len(values)

    isNegative = (finalValue < 0 or initialValue < 0) and -1 or 1
    growthRate = isNegative * (
        math.pow(abs(finalValue / initialValue), 1 / noOfValues) - 1
    )

    return growthRate


def getNetIncomeGrowthRate(stock: Stock, model: ValuationModel) -> Ratio:
    now = datetime.now()
    then = now - timedelta(days=(365 * model.yearsForEarningsCalcs))
    thenString = utils.dateToDateString(then)
    nowString = utils.dateToDateString(now)
    order = 1  # CFO using linear growth here but we normally use poly for growth rate

    initialValue = utils.getTrendEstimateForDate(
        stock.financialStatements.incomeStatements,
        "netIncome",
        IncomeStatement(),
        thenString,
        order,
    )
    finalValue = utils.getTrendEstimateForDate(
        stock.financialStatements.incomeStatements,
        "netIncome",
        IncomeStatement(),
        nowString,
        order,
    )

    if not initialValue or not finalValue:
        return 0

    growthRate = getGrowthRate([initialValue, finalValue]) / model.yearsForEarningsCalcs
    isNegative = growthRate < 0 and -1 or 1
    conservativeGrowthRate = growthRate * (1 - (isNegative * model.minMos))

    return conservativeGrowthRate


def getPriceGrowthRate(stock: Stock) -> Ratio:
    # get the price growth rate over the last year
    historicalValues = []
    now = datetime.now()
    aYearAgo = now - timedelta(days=365)
    aYearAgoString = utils.dateToDateString(aYearAgo)

    for date in stock.historicalPricing:
        if date > aYearAgoString:
            price = stock.historicalPricing[date].open

            if price:
                historicalValues.append(price)

    priceGrowthRate = getGrowthRate(historicalValues)

    return priceGrowthRate


def getNpv(futureValue: Currency, discountRate: Ratio, noYrs: int) -> Currency:
    if not noYrs:
        # if we aren't looking ahead, then the npv is the futureValue
        return futureValue

    # NPV = net present value
    return futureValue / math.pow(1 + discountRate, noYrs)


def getPeMultipleIv(
    eps: Ratio, avgPe: Ratio, growthRate: Ratio, discountRate: Ratio,
) -> Currency:
    noYrs = 5
    futureValue = eps * avgPe * math.pow(1 + growthRate, noYrs)

    return getNpv(futureValue, discountRate, noYrs)


def getGrahamIv(eps: Ratio, growthRate: Ratio, discountRate: Ratio) -> Currency:
    typicalPEForNoGrowthCompany = 7
    growthMultiplier = 1  # graham used 2 which is quite aggresive
    rrr = 4.4  # graham's risk free interest rate
    grahamIv = (
        eps
        * (typicalPEForNoGrowthCompany + growthMultiplier * growthRate * 100)
        * rrr
        / (discountRate * 100)
    )

    # a negative eps and growth rate will cancel each other out and appear positive
    isNegative = eps < 0 and growthRate < 0
    if isNegative:
        return grahamIv * -1

    return grahamIv


def getDcfIv(
    fcf: Currency,
    cash: Currency,
    liabilities: Currency,
    sharesOutstanding: Shares,
    growthRate: Ratio,
    declineRate: Ratio,
    discountRate: Ratio,
) -> Currency:
    # for 10 years, the fcf using growthRate and declineRate
    # then  the npv for each year and sum that
    # add the year 10 fcf multiplier of 12
    # add total npv, year 10's fcf, cash and subtract liabilities
    # divide by sharesOutstanding
    noYrs = 10
    futureFCFList: List[Currency] = []

    for i in range(noYrs):
        if i > 0:
            prevFCF = futureFCFList[i - 1]
        else:
            prevFCF = fcf

        declinePower = i + 1 - 1  # current year number - 1
        declineFactor = math.pow(1 - declineRate, declinePower)
        futureFCF = prevFCF * (1 + growthRate * declineFactor)

        futureFCFList.append(futureFCF)

    npvList = []

    for i in range(noYrs):
        npv = getNpv(futureFCFList[i], discountRate, i + 1)
        npvList.append(npv)

    totalNpv = sum(npvList)

    valuationLastFcf = 12  # 12 is conservative, 15 is aggressive
    npvList.reverse()
    year10FcfValue = npvList[0] * valuationLastFcf

    companyValue = totalNpv + year10FcfValue + cash - liabilities

    return companyValue / sharesOutstanding


def getRoeIv(
    equity: Currency,
    avgRoe: Ratio,
    sharesOutstanding: Shares,
    dividendYield: Ratio,
    growthRate: Ratio,
    discountRate: Ratio,
) -> Currency:
    equityPerShare = equity / sharesOutstanding
    noYrs = 10
    futureEquityPerShareList: List[Currency] = []

    for i in range(noYrs):
        if i > 0:
            prevEquityPerShare = futureEquityPerShareList[i - 1]
        else:
            prevEquityPerShare = equityPerShare

        futureEquityPerShare = prevEquityPerShare * (1 + growthRate)
        futureEquityPerShareList.append(futureEquityPerShare)

    futureDividendsPerShareList: List[Currency] = []

    for i in range(noYrs):
        if i > 0:
            prevDividendPerShare = futureDividendsPerShareList[i - 1]
        else:
            prevDividendPerShare = dividendYield

        futureDividendPerShare = prevDividendPerShare * (1 + growthRate)
        futureDividendsPerShareList.append(futureDividendPerShare)

    npvDividendList = []

    for i in range(noYrs):
        dividendPerShareNPV = getNpv(futureDividendsPerShareList[i], discountRate, i)
        npvDividendList.append(dividendPerShareNPV)

    futureEquityPerShareList.reverse()
    year10NetIncome = futureEquityPerShareList[0] * avgRoe

    requiredValue = year10NetIncome / (discountRate)

    npvRequiredValue = getNpv(requiredValue, discountRate, noYrs)

    npvDividends = sum(npvDividendList)

    return npvRequiredValue + npvDividends


def getLiquidationIv(equity: Currency, sharesOutstanding: Shares) -> Currency:
    return equity / sharesOutstanding


def getAltmanZScore(
    assets: Currency,
    liabilities: Currency,
    retainedEarnings: Currency,
    earningsBeforeInterestAndTax: Currency,
    totalRevenue: Currency,
) -> Ratio:
    equity = getEquity(assets, liabilities)

    if not liabilities or not totalRevenue:
        return 0

    return (
        1.2 * equity / assets
        + 1.4 * retainedEarnings / assets
        + 3.3 * earningsBeforeInterestAndTax / assets
        + 0.6 * equity / liabilities
        + 1 * totalRevenue / assets
    )


def getLatestValidFinancialStatement(statements, validator):
    latestDate = ""

    for date in statements:
        latestDate = utils.getLargest(date, latestDate)

    if not latestDate:
        return None

    statement = statements[latestDate]
    isValid = validator(statement)

    if isValid:
        return statement

    # remove the invalid date and try again
    del statements[latestDate]
    return getLatestValidFinancialStatement(statements, validator)


def validateBalanceSheet(balanceSheet: BalanceSheet) -> bool:
    # TODO is retainedEarnings, currentLiabilities ever 0
    if (
        not balanceSheet
        or not balanceSheet.assets
        or not balanceSheet.currentAssets
        or not balanceSheet.liabilities
        or not balanceSheet.currentLiabilities
        or not balanceSheet.retainedEarnings
        or not balanceSheet.cash
    ):
        return False

    return True


def getStatementYears(stock: Stock):
    return min(
        math.floor(len(stock.financialStatements.incomeStatements) / 4),
        math.floor(len(stock.financialStatements.balanceSheets) / 4),
        math.floor(len(stock.financialStatements.cashFlowStatements) / 4),
    )


def getAvgPe(stock):
    peList = []
    historicalNetIncomes = utils.getHistoricalValuesFromFinancialStatements(
        stock.financialStatements.incomeStatements,
        "netIncome",
        ValuationModel.yearsForEarningsCalcs * 4,
    )

    for item in historicalNetIncomes:
        # TODO we should do this for each statements's sharesOutstanding but we don't have that info
        historicalEps = getEps(item["value"], stock.sharesOutstanding)
        historicalPe = getPe(stock.currentPrice, historicalEps)
        peList.append(historicalPe)

    if len(peList) == 0:
        return 0

    avgPe = 4 * sum(peList) / len(peList)  # x4 to get yearly value

    return avgPe


def getDividendYieldForYear(stock):
    historicalDividends = utils.getHistoricalValuesFromFinancialStatements(
        stock.financialStatements.cashFlowStatements, "dividendsPaid", 4
    )

    dividendsPaidInLastYear = 0
    for item in historicalDividends:
        dividendsPaidInLastYear = dividendsPaidInLastYear + item["value"]

    dividendYield = getDividendYield(
        dividendsPaidInLastYear, stock.sharesOutstanding, stock.currentPrice,
    )

    return dividendYield


def getFcfForYear(stock):
    historicalCashFromOperations = utils.getHistoricalValuesFromFinancialStatements(
        stock.financialStatements.cashFlowStatements, "cashFromOperations", 4
    )
    historicalCapex = utils.getHistoricalValuesFromFinancialStatements(
        stock.financialStatements.cashFlowStatements, "capex", 4
    )

    fcfForYear = 0
    for i in range(len(historicalCashFromOperations)):
        fcfForQuarter = getFcf(
            historicalCashFromOperations[i]["value"], historicalCapex[i]["value"]
        )
        fcfForYear = fcfForYear + fcfForQuarter

    return fcfForYear


def getNetIncomeForYear(stock):
    historicalNetIncomes = utils.getHistoricalValuesFromFinancialStatements(
        stock.financialStatements.incomeStatements, "netIncome", 4
    )

    netIncomeForYear = 0
    for i in range(len(historicalNetIncomes)):
        netIncomeForQuarter = historicalNetIncomes[i]["value"]
        netIncomeForYear = netIncomeForYear + netIncomeForQuarter

    return netIncomeForYear


def getNetIncomeAvg(stock, years):
    historicalNetIncomes = utils.getHistoricalValuesFromFinancialStatements(
        stock.financialStatements.incomeStatements,
        "netIncome",
        years * 4,  # * 4 quarters
    )

    totalNetIncome = 0
    for i in range(len(historicalNetIncomes)):
        netIncomeForQuarter = historicalNetIncomes[i]["value"]
        totalNetIncome = totalNetIncome + netIncomeForQuarter

    return totalNetIncome / years


def getTotalRevenueForYear(stock):
    historicalTotalRevenue = utils.getHistoricalValuesFromFinancialStatements(
        stock.financialStatements.incomeStatements, "totalRevenue", 4
    )

    totalRevenueForYear = 0
    for i in range(len(historicalTotalRevenue)):
        totalRevenueForQuarter = historicalTotalRevenue[i]["value"]
        totalRevenueForYear = totalRevenueForYear + totalRevenueForQuarter

    return totalRevenueForYear


def getEarningsBeforeInterestAndTaxForYear(stock):
    historicalIncomeBeforeTax = utils.getHistoricalValuesFromFinancialStatements(
        stock.financialStatements.incomeStatements, "incomeBeforeTax", 4
    )
    historicalInterestExpense = utils.getHistoricalValuesFromFinancialStatements(
        stock.financialStatements.incomeStatements, "interestExpense", 4
    )
    historicalInterestIncome = utils.getHistoricalValuesFromFinancialStatements(
        stock.financialStatements.incomeStatements, "interestIncome", 4
    )

    earningsBeforeInterestAndTaxForYear = 0
    for i in range(len(historicalIncomeBeforeTax)):
        earningsBeforeInterestAndTaxForQuarter = historicalIncomeBeforeTax[i] and (
            historicalIncomeBeforeTax[i]["value"]
            or 0 - i in historicalInterestExpense
            and abs(historicalInterestExpense[i]["value"])
            or 0 + i in historicalInterestIncome
            and abs(historicalInterestIncome[i]["value"])
            or 0
        )
        earningsBeforeInterestAndTaxForYear = (
            earningsBeforeInterestAndTaxForYear + earningsBeforeInterestAndTaxForQuarter
        )

    return earningsBeforeInterestAndTaxForYear


def getValuation(stock: Stock, model: ValuationModel) -> Valuation:
    latestBalanceSheet: BalanceSheet = getLatestValidFinancialStatement(
        stock.financialStatements.balanceSheets, validateBalanceSheet
    )

    if not validateBalanceSheet(latestBalanceSheet):
        print(
            f"Latest balance sheet is not valid for {stock.symbol}",
            latestBalanceSheet,
            "\n",
        )
        return Valuation()

    netIncomeAvg = getNetIncomeAvg(stock, model.yearsForEarningsCalcs)
    assets = latestBalanceSheet.assets
    liabilities = latestBalanceSheet.liabilities
    equity = getEquity(assets, liabilities)
    roe = getRoe(netIncomeAvg, equity)
    roa = getRoa(netIncomeAvg, assets)
    dividendYield = getDividendYieldForYear(stock)
    fcf = getFcfForYear(stock)
    marketCap = getMarketCap(stock.sharesOutstanding, stock.currentPrice)
    eps = getEps(netIncomeAvg, stock.sharesOutstanding)
    pe = getPe(stock.currentPrice, eps)
    growthRate = getNetIncomeGrowthRate(stock, model)
    priceGrowthRate = getPriceGrowthRate(stock)
    peg = getPeg(pe, growthRate)
    totalRevenue = getTotalRevenueForYear(stock)
    earningsBeforeInterestAndTax = getEarningsBeforeInterestAndTaxForYear(stock)
    statementYears = getStatementYears(stock)
    pb = customRound(getPb(stock.currentPrice, equity, stock.sharesOutstanding), 2)
    blendedMultiplier = pe * pb
    currentLiabilities = customRound(latestBalanceSheet.currentLiabilities, 2)
    altmanZScore = getAltmanZScore(
        assets,
        liabilities,
        latestBalanceSheet.retainedEarnings,
        earningsBeforeInterestAndTax,
        totalRevenue,
    )
    peMultipleIv = getPeMultipleIv(eps, pe, growthRate, model.discountRate)
    grahamIv = getGrahamIv(eps, growthRate, model.discountRate)
    dcfIv = getDcfIv(
        fcf,
        latestBalanceSheet.cash,
        currentLiabilities,
        stock.sharesOutstanding,
        growthRate,
        model.declineRate,
        model.discountRate,
    )
    roeIv = getRoeIv(
        equity,
        roe,
        stock.sharesOutstanding,
        dividendYield,
        growthRate,
        model.discountRate,
    )
    liquidationIv = getLiquidationIv(equity, stock.sharesOutstanding)

    valuation = Valuation()
    valuation.dividendYield = customRound(dividendYield, 2)
    valuation.marketCap = customRound(marketCap, 2)
    valuation.roe = customRound(roe, 2)
    valuation.roa = customRound(roa, 2)
    valuation.growthRate = customRound(growthRate, 2)
    valuation.priceGrowthRate = customRound(priceGrowthRate, 2)
    valuation.dte = customRound(getDte(currentLiabilities, equity), 2)
    valuation.cr = customRound(getCr(assets, currentLiabilities), 2)
    valuation.eps = customRound(eps, 2)
    valuation.pe = customRound(pe, 2)
    valuation.peg = customRound(peg, 2)
    valuation.pb = customRound(pb, 2)
    valuation.blendedMultiplier = customRound(blendedMultiplier, 2)
    valuation.fcf = customRound(fcf, 2)
    valuation.altmanZScore = customRound(altmanZScore, 2,)
    valuation.statementYears = statementYears
    valuation.peMultipleIv = customRound(peMultipleIv, 2)
    valuation.grahamIv = customRound(grahamIv, 2)
    valuation.dcfIv = customRound(dcfIv, 2,)
    valuation.roeIv = customRound(roeIv, 2,)
    valuation.liquidationIv = customRound(liquidationIv, 2)

    return valuation


def getFairValue(valuation: Valuation) -> Currency:
    return valuation.peMultipleIv


def getViability(valuation: Valuation, model: ValuationModel) -> bool:
    # check to see if the stock and it's valuation meets our minimum requirements
    if (
        valuation.roe < model.minRoe
        or valuation.growthRate < model.minGrowthRate
        or valuation.dte > model.maxDte
        or valuation.dte < 0
        or valuation.dte < 0
        or valuation.cr < model.minCr
        or valuation.eps < model.minEps
        or valuation.pe > model.maxPe
        or valuation.pe < 0
        or valuation.peg > model.maxPeg
        or valuation.peg < 0
        or not valuation.peg
        or valuation.pb > model.maxPb
        or valuation.pb < 0
        or valuation.blendedMultiplier > model.maxBlendedMultiplier
        or valuation.blendedMultiplier <= 0
        or not valuation.blendedMultiplier
        or valuation.altmanZScore < model.minAltmanZScore
        or valuation.statementYears < model.minStatementYears
    ):
        return False

    return True


def getExpectedReturn(valuation: Valuation, currentPrice: Currency) -> Currency:
    return customRound(100 * (valuation.fairValue - currentPrice) / currentPrice, 2)


def getInstruction(
    valuation: Valuation, currentPrice: Currency, model: ValuationModel
) -> str:
    stockIsViable = getViability(valuation, model)
    stockIsUndervalued = currentPrice <= valuation.fairValue
    stockIsOvervalued = currentPrice >= valuation.fairValue

    if not stockIsViable or stockIsOvervalued:
        return "SELL"
    elif stockIsViable and stockIsUndervalued:
        return "BUY"
    else:
        return "HOLD"


def getHealth(valuation: Valuation) -> str:
    if valuation.altmanZScore < 1.8:
        return "DYING"
    elif valuation.altmanZScore >= 3.0:
        return "HEALTHY"
    else:
        return "AVERAGE"


def evaluate(stock: Stock) -> Valuation:
    model = ValuationModel()

    # get the valuation
    valuation = getValuation(stock, model)

    # evaluate/assess the valuation
    valuation.fairValue = getFairValue(valuation)
    valuation.expectedReturn = getExpectedReturn(valuation, stock.currentPrice)
    valuation.instruction = getInstruction(valuation, stock.currentPrice, model)
    valuation.health = getHealth(valuation)

    return valuation


def evaluateStock(symbol: Symbol, exchange: str, dateString: str = ""):
    filepath = f"data/stocks/{exchange}/{symbol}.json"
    with open(filepath) as file:
        stock = typedload.load(json.load(file), Stock)

    if dateString:
        date = utils.dateStringToDate(dateString)
        filepath = f"data/tempSnapshots/{dateString}/{exchange}/{symbol}.json"  # filepath to save new snapshot
        stock = getStockSnapshot(stock, date)

    valuation = evaluate(stock)
    stock.valuation = valuation

    with utils.safeOpenWrite(filepath) as file:
        jsonString = json.dumps(stock, default=lambda o: o.__dict__, indent=2)
        file.write(jsonString)

    print(f"{symbol} added to {filepath}")


def evaluateManager():
    # parse args
    argParser = argparse.ArgumentParser()
    argParser.add_argument("--stock", type=str)
    argParser.add_argument("--exchange", type=str)
    argParser.add_argument("--date", type=str)
    argParser.add_argument("--allStocks", type=bool)
    args = argParser.parse_known_args()

    stock = args[0].stock
    exchange = args[0].exchange
    date = args[0].date
    allStocks = args[0].allStocks

    if stock and exchange and date:
        evaluateStock(stock, exchange, date)

    if exchange and allStocks:
        # get a list of the stocks and evaluate each one
        stocks = getStockList(exchange)

        for symbol in stocks:
            evaluateStock(symbol, exchange)


evaluateManager()
