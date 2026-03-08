import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import StandaloneSandbox from './standalone-sandbox'
import { ThemeProvider } from './contexts'

createRoot(document.getElementById('root')).render(
  <StrictMode><ThemeProvider><StandaloneSandbox /></ThemeProvider></StrictMode>
)
