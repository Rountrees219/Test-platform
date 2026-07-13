// API utility functions
import axios from 'axios';
import type { MatchupState, TimelineEvent, GoldLedger, SimulationStatus } from '../types';

// Use the proxy path in dev (Vite proxies /api → backend)
// In production, set VITE_API_URL to backend URL
const BASE_URL = import.meta.env.VITE_API_URL || '';

const api = axios.create({
  baseURL: BASE_URL,
  timeout: 10000,
});

export const fetchDemoMatchup = async (): Promise<MatchupState> => {
  const { data } = await api.get('/api/matchup/demo');
  return data;
};

export const fetchMatchup = async (id: number): Promise<MatchupState> => {
  const { data } = await api.get(`/api/matchup/${id}`);
  return data;
};

export const fetchTimeline = async (
  matchupId: number,
  filters?: { type?: string; team_id?: number }
): Promise<{ matchup_id: number; events: TimelineEvent[] }> => {
  const params = new URLSearchParams();
  if (filters?.type) params.append('event_type', filters.type);
  if (filters?.team_id) params.append('team_id', String(filters.team_id));
  const { data } = await api.get(`/api/events/matchup/${matchupId}/timeline?${params}`);
  return data;
};

export const fetchGoldLedger = async (teamId: number): Promise<GoldLedger> => {
  const { data } = await api.get(`/api/gold/team/${teamId}/ledger`);
  return data;
};

export const fetchGoldSummary = async (matchupId: number) => {
  const { data } = await api.get(`/api/gold/matchup/${matchupId}/summary`);
  return data;
};

export const fetchAuditReport = async (matchupId: number) => {
  const { data } = await api.get(`/api/admin/matchup/${matchupId}/audit-report`);
  return data;
};

export const fetchRules = async () => {
  const { data } = await api.get('/api/admin/rules');
  return data;
};

export const fetchAmbiguities = async () => {
  const { data } = await api.get('/api/admin/ambiguities');
  return data;
};

export const seedDatabase = async () => {
  const { data } = await api.post('/api/admin/seed');
  return data;
};

export const controlSimulation = async (
  matchupId: number,
  action: 'start' | 'pause' | 'resume' | 'stop' | 'restart' | 'jump_to_end',
  speed?: number
) => {
  const { data } = await api.post(`/api/simulation/${matchupId}/control`, { action, speed });
  return data;
};

export const fetchSimulationStatus = async (matchupId: number): Promise<SimulationStatus> => {
  const { data } = await api.get(`/api/simulation/${matchupId}/status`);
  return data;
};

export const getSSEUrl = (matchupId: number) => `${BASE_URL || window.location.origin}/api/simulation/${matchupId}/sse`;

export default api;
