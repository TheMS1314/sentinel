import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import RiskIntelligence from '../components/RiskIntelligence';
import { api } from '../api';
vi.mock('../api', () => ({api: {riskOverview: vi.fn(), saveRiskGuard: vi.fn(), saveMerchantRisk: vi.fn(), saveThreatIndicator: vi.fn()}}));
const overview = (version = 0) => ({guard:{version,frozen:false,window_seconds:60,max_dispatches:null,max_amount_paise:null},
  current_indicators:0,merchant_profiles:0,pending_payments:2,events:[]});
afterEach(() => {cleanup();vi.resetAllMocks();});

it('labels commercial providers as disconnected and local evidence as uncertain', async () => {
  api.riskOverview.mockResolvedValue(overview());
  render(<RiskIntelligence/>);
  await screen.findByText('Emergency payment guard');
  expect(screen.getByText(/Mastercard Decision Intelligence, Recorded Future and RiskRecon are not connected/)).toBeTruthy();
  expect(screen.getByText(/Missing evidence is unknown/)).toBeTruthy();
  expect(screen.getByLabelText('Profile source').value).toBe('synthetic');
});

it('submits a versioned freeze and converts rupees to exact integer paise', async () => {
  api.riskOverview.mockResolvedValueOnce(overview()).mockResolvedValue(overview(1));
  api.saveRiskGuard.mockResolvedValue({});
  render(<RiskIntelligence/>);
  await screen.findByText('Emergency payment guard');
  fireEvent.click(screen.getByLabelText('Freeze payment dispatch'));
  fireEvent.change(screen.getByLabelText('Maximum dispatch amount (₹; blank = unlimited)'), {target:{value:'29.29'}});
  fireEvent.change(screen.getByLabelText('Reason for guard change'), {target:{value:'Investigating an incident'}});
  fireEvent.click(screen.getByRole('button', {name:'Save payment guard'}));
  await waitFor(() => expect(api.saveRiskGuard).toHaveBeenCalledWith({expected_version:0,frozen:true,
    window_seconds:60,max_dispatches:null,max_amount_paise:2929,reason:'Investigating an incident'}));
  await screen.findByRole('status');
});

it('shows a configuration conflict without falsely reporting success', async () => {
  api.riskOverview.mockResolvedValue(overview());
  api.saveRiskGuard.mockRejectedValue(new Error('Guard configuration changed; refresh before saving'));
  render(<RiskIntelligence/>);
  await screen.findByText('Emergency payment guard');
  fireEvent.change(screen.getByLabelText('Reason for guard change'), {target:{value:'Freeze'}});
  fireEvent.click(screen.getByRole('button', {name:'Save payment guard'}));
  expect((await screen.findByRole('alert')).textContent).toContain('Guard configuration changed');
  expect(screen.queryByRole('status')).toBeNull();
});

it('submits expiring synthetic merchant evidence without a claimed provider connection', async () => {
  api.riskOverview.mockResolvedValue(overview()); api.saveMerchantRisk.mockResolvedValue({});
  render(<RiskIntelligence/>);
  await screen.findByText('Emergency payment guard');
  const values = {'Profile owner ID':'owner_demo','Merchant name':'FreshMart','Profile domain (exact hostname)':'shop.example',
    'Profile evidence/reason':'Demo profile', 'Profile expires (your local time)':'2099-01-01T12:00'};
  for (const [name,value] of Object.entries(values)) fireEvent.change(screen.getByLabelText(name), {target:{value}});
  fireEvent.click(screen.getByRole('button', {name:'Save merchant profile'}));
  await waitFor(() => expect(api.saveMerchantRisk).toHaveBeenCalledWith(expect.objectContaining({owner_id:'owner_demo',
    merchant_name:'FreshMart',domain:'shop.example',source:'synthetic',evidence:'Demo profile',cyber_risk_score:0})));
});
