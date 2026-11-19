import { StrictMode, Suspense, lazy } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import { Boundary } from './components/Boundary';
import { Defs } from './components/Defs';
import { readTreePop } from './lib/popout';
import './styles.css';

// The process tree popped out of the inspector is a page of its own, loaded
// only when it is the one asked for.
const TreePopout = lazy(() => import('./tabs/TreePopout'));

const el = document.getElementById('root');
if (!el) throw new Error('#root missing from index.html');

// A throw anywhere a tab's or a panel's own boundary does not catch -- the
// shell, the header, the filter -- says so, rather than leaving a white page.
createRoot(el).render(
  <StrictMode>
    <Defs />
    <Boundary what="the page">
      {readTreePop(window.location.search) ? (
        <Suspense fallback={null}>
          <TreePopout />
        </Suspense>
      ) : (
        <App />
      )}
    </Boundary>
  </StrictMode>,
);
