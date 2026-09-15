import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import { Defs } from './components/Defs';
import './styles.css';

const el = document.getElementById('root');
if (!el) throw new Error('#root missing from index.html');

createRoot(el).render(
  <StrictMode>
    <Defs />
    <App />
  </StrictMode>,
);
