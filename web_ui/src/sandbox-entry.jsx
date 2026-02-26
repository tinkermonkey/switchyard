import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import StandaloneSandbox from './standalone-sandbox'

createRoot(document.getElementById('root')).render(
  <StrictMode><StandaloneSandbox /></StrictMode>
)
