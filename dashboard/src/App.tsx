import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { OverviewPage } from "./pages/OverviewPage";
import { MarketsPage } from "./pages/MarketsPage";
import { AssetDetailPage } from "./pages/AssetDetailPage";
import { SignalsPage } from "./pages/SignalsPage";
import { SignalDetailPage } from "./pages/SignalDetailPage";
import { PortfolioPage } from "./pages/PortfolioPage";
import { TradesPage } from "./pages/TradesPage";
import { RiskPage } from "./pages/RiskPage";
import { BacktestsPage } from "./pages/BacktestsPage";
import { SystemPage } from "./pages/SystemPage";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
      staleTime: 5_000,
    },
  },
});

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<OverviewPage />} />
            <Route path="markets" element={<MarketsPage />} />
            <Route path="markets/:assetId" element={<AssetDetailPage />} />
            <Route path="signals" element={<SignalsPage />} />
            <Route path="signals/:signalId" element={<SignalDetailPage />} />
            <Route path="portfolio" element={<PortfolioPage />} />
            <Route path="trades" element={<TradesPage />} />
            <Route path="risk" element={<RiskPage />} />
            <Route path="backtests" element={<BacktestsPage />} />
            <Route path="system" element={<SystemPage />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
