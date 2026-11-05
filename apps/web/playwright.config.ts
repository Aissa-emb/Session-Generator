import {defineConfig} from '@playwright/test';
// SG_UI_PORT lets a second checkout run the suite without sharing the test server.
const port = process.env.SG_UI_PORT || '8001';
const state = port === '8001' ? '.state/ui-tests' : `.state/ui-tests-${port}`;
export default defineConfig({testDir:'./tests',timeout:60000,workers:1,use:{baseURL:`http://127.0.0.1:${port}`,viewport:{width:1440,height:1000},screenshot:'only-on-failure',trace:'retain-on-failure'},webServer:{command:`cd ../.. && SG_DATA_DIR=${state} SG_SECRET_DIR=${state}/secrets .venv/bin/python -m uvicorn server.app:app --host 127.0.0.1 --port ${port}`,url:`http://127.0.0.1:${port}/health`,reuseExistingServer:false,timeout:30000}});
