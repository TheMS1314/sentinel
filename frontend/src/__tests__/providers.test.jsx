import {afterEach,expect,it,vi} from 'vitest';
import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import ProviderConnections from '../components/ProviderConnections';
import {api} from '../api';
vi.mock('../api',()=>({api:{providers:vi.fn(),bindProvider:vi.fn(),refreshProvider:vi.fn()}}));
afterEach(()=>{cleanup();vi.resetAllMocks();});
it('does not confuse configured credentials with verified commercial evidence',async()=>{
 api.providers.mockResolvedValue({configuration:{recorded_future:'configured_unverified',riskrecon:'not_configured'},bindings:[]});
 render(<ProviderConnections/>);
 expect(await screen.findByText('recorded_future: configured_unverified')).toBeTruthy();
 expect(screen.getByText(/licensed access/)).toBeTruthy();
});
it('shows a missing provider credential after refresh without reporting success',async()=>{
 api.providers.mockResolvedValue({configuration:{recorded_future:'not_configured'},bindings:[{binding_id:'b1',merchant_id:'m1',provider:'recorded_future',external_id:'idn:example.com',version:1}]});
 api.refreshProvider.mockResolvedValue({status:'not_configured',error:'Provider credential missing'});
 render(<ProviderConnections/>);
 fireEvent.click(await screen.findByRole('button',{name:'Refresh b1'}));
 await waitFor(()=>expect(api.refreshProvider).toHaveBeenCalledWith('b1'));
 expect((await screen.findByRole('status')).textContent).toContain('not_configured');
});
it('requires explicit binding opt-in and shows a disabled scheduling worker',async()=>{
 api.providers.mockResolvedValue({scheduler_enabled:false,configuration:{},bindings:[]});api.bindProvider.mockResolvedValue({binding_id:'new',version:1});
 render(<ProviderConnections/>);
 await screen.findByText(/disabled by deployment configuration/);
 expect(screen.getByLabelText('Automatically refresh this binding').checked).toBe(false);
 fireEvent.change(screen.getByLabelText('Binding merchant ID'),{target:{value:'m1'}});
 fireEvent.change(screen.getByLabelText('External entity ID'),{target:{value:'idn:example.com'}});
 fireEvent.click(screen.getByLabelText('Automatically refresh this binding'));
 fireEvent.change(screen.getByLabelText('Refresh interval in seconds'),{target:{value:'600'}});
 fireEvent.click(screen.getByRole('button',{name:'Save provider binding'}));
 await waitFor(()=>expect(api.bindProvider).toHaveBeenCalledWith(expect.objectContaining({auto_refresh:true,refresh_interval_seconds:600,expected_version:0})));
});
it('prevents a second refresh while a lease is active',async()=>{
 api.providers.mockResolvedValue({scheduler_enabled:true,configuration:{},bindings:[{binding_id:'b1',merchant_id:'m',provider:'recorded_future',version:1,auto_refresh:true,refresh_in_progress:true,refresh_status:'processing'}]});
 render(<ProviderConnections/>);
 expect((await screen.findByRole('button',{name:'Refresh b1'})).disabled).toBe(true);
});
