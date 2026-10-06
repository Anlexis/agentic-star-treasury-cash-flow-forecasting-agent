"""AgentCore Platform v1.0"""

# Manifest entry point. config/agent.yaml declares the dotted class path
# src.graph.graph.CorporateTreasuryCashFlowForecastingAgent; re-exporting the
# class here additionally lets loaders resolve it directly from the src.graph
# package.

from .graph import CorporateTreasuryCashFlowForecastingAgent

__all__ = ["CorporateTreasuryCashFlowForecastingAgent"]
